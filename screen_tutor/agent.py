import argparse
import base64
import io
import os
import queue
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Deque, List, Optional, Tuple

from dotenv import load_dotenv
from mss import mss
from PIL import Image
from pynput import keyboard
from openai import OpenAI
import pyttsx3

SYSTEM_PROMPT = """
Você é o “Tutor de Tela”, um assistente visual em tempo real que age como um professor ao lado do usuário.
Você observa a tela continuamente através de frames (capturas de tela) fornecidos pela ferramenta de visão.
Você NÃO responde automaticamente: só fala quando o usuário acionar o comando de ajuda (ex.: “Tutor, analise a tela” / hotkey / botão Perguntar).
Quando acionado, você analisa o frame mais recente e, se disponível, um curto histórico de frames (últimos 10–30s) para entender o contexto.

OBJETIVO
1) Entender o que está na tela (UI, textos, botões, estados do jogo/app).
2) Responder de forma prática e acionável: o usuário quer “o que fazer agora”.
3) Fornecer uma saída curta ideal para voz (TTS) + detalhes em texto quando útil.

COMPORTAMENTO GERAL
- Seja direto, específico e orientado a passos.
- Se houver ambiguidade (ex.: vários botões parecidos), peça uma micro-confirmação usando referências visuais (“No canto superior direito, há um botão X. É esse?”), mas tente reduzir perguntas ao mínimo.
- Se você não tiver certeza, declare o grau de confiança (“Acho que…”, “Parece que…”), e ofereça um passo de verificação.

PRIVACIDADE E SEGURANÇA (REGRA CRÍTICA)
- NÃO transcreva nem repita: senhas, códigos 2FA, números completos de cartão, dados bancários, CPF/RG, endereços completos, e-mails pessoais, chaves privadas, tokens.
- Se esses dados aparecerem na tela ou forem pedidos: responda com uma recusa breve e peça para o usuário ocultar/fechar a parte sensível antes de continuar.
- Não oriente ações ilícitas, trapaças competitivas, invasão de contas, phishing, exploração maliciosa ou engenharia social.

MODO “SEMPRE ATIVO” (APENAS OBSERVANDO)
- Você pode manter um resumo curto do estado atual (ex.: “usuário no menu X”, “fase Y”, “janela Z aberta”).
- Não armazene conteúdo sensível e não faça log detalhado da tela. Use apenas memória temporária.

FORMATO DE RESPOSTA (OBRIGATÓRIO)
Sempre responda em duas seções:

1) VOZ (TTS) — 1 a 3 frases, linguagem simples, sem termos técnicos desnecessários.
2) DETALHES — passos numerados, com referências visuais (posição, cor, ícone, rótulo do botão), alternativas e alertas.

EXEMPLOS DE INTENÇÃO
- Jogos: estratégia de próximo passo, leitura de objetivos, dica de build, onde clicar, o que evitar.
- Apps: onde clicar, como configurar, como resolver erro.
- “Conta” na tela: se for expressão matemática visível, calcule e explique rapidamente. Se for “conta bancária”/dados pessoais, recuse e peça para ocultar.

FLUXO DE TRABALHO
Ao receber um comando de ajuda:
1) Analise o frame atual (e histórico curto se disponível).
2) Identifique: aplicativo/jogo, objetivo do usuário, elementos relevantes (botões/menus/alertas).
3) Monte uma ação recomendada (passos).
4) Gere “VOZ (TTS)” curto e “DETALHES” completos.
""".strip()


@dataclass
class Frame:
    timestamp: float
    image: Image.Image


class FrameBuffer:
    def __init__(self, max_seconds: int) -> None:
        self.max_seconds = max_seconds
        self.frames: Deque[Frame] = deque()
        self.lock = threading.Lock()

    def add(self, image: Image.Image) -> None:
        now = time.time()
        with self.lock:
            self.frames.append(Frame(timestamp=now, image=image))
            self._trim(now)

    def latest_with_history(self) -> Tuple[Optional[Frame], List[Frame]]:
        now = time.time()
        with self.lock:
            self._trim(now)
            if not self.frames:
                return None, []
            latest = self.frames[-1]
            history = list(self.frames)
        return latest, history

    def _trim(self, now: float) -> None:
        while self.frames and (now - self.frames[0].timestamp) > self.max_seconds:
            self.frames.popleft()


class ScreenCapture(threading.Thread):
    def __init__(self, fps: int, buffer: FrameBuffer, stop_event: threading.Event) -> None:
        super().__init__(daemon=True)
        self.fps = fps
        self.buffer = buffer
        self.stop_event = stop_event

    def run(self) -> None:
        interval = 1.0 / max(self.fps, 1)
        with mss() as sct:
            monitor = sct.monitors[1]
            while not self.stop_event.is_set():
                raw = sct.grab(monitor)
                image = Image.frombytes("RGB", raw.size, raw.rgb)
                self.buffer.add(image)
                time.sleep(interval)


class HotkeyListener(threading.Thread):
    def __init__(self, hotkey: str, trigger_queue: queue.Queue, stop_event: threading.Event) -> None:
        super().__init__(daemon=True)
        self.hotkey = hotkey
        self.trigger_queue = trigger_queue
        self.stop_event = stop_event
        self._listener: Optional[keyboard.GlobalHotKeys] = None

    def run(self) -> None:
        def on_activate() -> None:
            self.trigger_queue.put(time.time())

        self._listener = keyboard.GlobalHotKeys({self.hotkey: on_activate})
        self._listener.start()
        while not self.stop_event.is_set():
            time.sleep(0.1)
        if self._listener:
            self._listener.stop()


class ScreenTutorAgent:
    def __init__(
        self,
        client: OpenAI,
        model: str,
        buffer: FrameBuffer,
        tts_enabled: bool,
    ) -> None:
        self.client = client
        self.model = model
        self.buffer = buffer
        self.tts_enabled = tts_enabled
        self.tts_engine = pyttsx3.init() if tts_enabled else None

    def handle_request(self) -> str:
        latest, history = self.buffer.latest_with_history()
        if latest is None:
            return "Nenhuma captura disponível ainda. Aguarde alguns segundos e tente novamente."

        images = [latest.image]
        history_images = [frame.image for frame in history[:-1]]
        if history_images:
            images.extend(history_images[-3:])

        prompt = self._build_prompt(images)
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
        )
        message = response.choices[0].message.content or ""
        if self.tts_enabled:
            self._speak(message)
        return message

    def _build_prompt(self, images: List[Image.Image]) -> List[dict]:
        content: List[dict] = [
            {
                "type": "text",
                "text": (
                    "Analise a tela atual e o breve histórico. Responda seguindo o formato\n"
                    "VOZ (TTS) e DETALHES, respeitando as regras de privacidade."
                ),
            }
        ]
        for idx, image in enumerate(images):
            content.append(
                {
                    "type": "text",
                    "text": f"Frame {idx + 1} (mais recente primeiro):",
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_to_data_url(image)},
                }
            )
        return content

    def _speak(self, message: str) -> None:
        if not self.tts_engine:
            return
        voice_text = extract_voice_section(message)
        if not voice_text:
            voice_text = message
        self.tts_engine.say(voice_text)
        self.tts_engine.runAndWait()


def image_to_data_url(image: Image.Image) -> str:
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    encoded = base64.b64encode(buffer.getvalue()).decode("utf-8")
    return f"data:image/png;base64,{encoded}"


def extract_voice_section(message: str) -> str:
    for line in message.splitlines():
        if line.strip().lower().startswith("voz"):
            return line.split(":", 1)[-1].strip()
    return ""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Tutor de Tela (Screen Tutor)")
    parser.add_argument("--fps", type=int, default=2, help="Frames por segundo para captura.")
    parser.add_argument(
        "--history-seconds",
        type=int,
        default=20,
        help="Janela de histórico (em segundos).",
    )
    parser.add_argument(
        "--hotkey",
        type=str,
        default="<ctrl>+<shift>+h",
        help="Atalho global para acionar o tutor.",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=os.getenv("OPENAI_MODEL", "gpt-4o-mini"),
        help="Modelo OpenAI compatível com visão.",
    )
    parser.add_argument(
        "--no-tts",
        action="store_true",
        help="Desativa a saída de voz (TTS).",
    )
    return parser.parse_args()


def main() -> None:
    load_dotenv()
    args = parse_args()

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise SystemExit("Defina OPENAI_API_KEY no ambiente antes de rodar.")

    client = OpenAI(
        api_key=api_key,
        base_url=os.getenv("OPENAI_BASE_URL"),
    )

    buffer = FrameBuffer(max_seconds=args.history_seconds)
    stop_event = threading.Event()
    trigger_queue: queue.Queue = queue.Queue()

    capture_thread = ScreenCapture(args.fps, buffer, stop_event)
    hotkey_thread = HotkeyListener(args.hotkey, trigger_queue, stop_event)

    capture_thread.start()
    hotkey_thread.start()

    agent = ScreenTutorAgent(
        client=client,
        model=args.model,
        buffer=buffer,
        tts_enabled=not args.no_tts,
    )

    print("Tutor de Tela em execução. Pressione o atalho para pedir ajuda.")
    try:
        while True:
            trigger_queue.get()
            print("\nAnalisando a tela...\n")
            response = agent.handle_request()
            print(response)
    except KeyboardInterrupt:
        stop_event.set()
        print("Encerrando...")


if __name__ == "__main__":
    main()
