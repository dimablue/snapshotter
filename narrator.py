"""Describe a captured region with Claude, then speak it with macOS `say`.

Runs in a background thread so the overlay never freezes. Needs ANTHROPIC_API_KEY.
"""
import base64
import subprocess
import threading

PROMPT = (
    "This image is a region of a computer screen that the user just captured by looking at it "
    "and blinking. Describe what it shows in one or two short spoken sentences, under 40 words. "
    "If it contains key text, numbers or a chart trend, say the important part. "
    "No preamble, no markdown - this is read aloud."
)


class Narrator:
    def __init__(self, model="claude-opus-5-5", voice=None, on_text=None):
        import anthropic  # imported lazily so the app runs without the SDK

        self.anthropic = anthropic
        self.client = anthropic.Anthropic()
        self.model = model
        self.voice = voice
        self.on_text = on_text or (lambda text: None)
        self._say = None

    def describe_async(self, png_path):
        threading.Thread(target=self._describe, args=(png_path,), daemon=True).start()

    def _describe(self, png_path):
        try:
            text = self.describe(png_path)
        except self.anthropic.APIConnectionError:
            text = "I couldn't reach Claude."
        except self.anthropic.AuthenticationError:
            text = "Claude API key is missing or invalid."
        except self.anthropic.APIStatusError as e:
            text = f"Claude returned an error, status {e.status_code}."
        except Exception as e:  # e.g. no credentials configured at all
            print(f"[snapshotter] narration failed: {e}", flush=True)
            text = "Narration failed. Is ANTHROPIC_API_KEY set?"
        self.on_text(text)
        self.speak(text)

    def describe(self, png_path):
        with open(png_path, "rb") as f:
            data = base64.standard_b64encode(f.read()).decode("utf-8")
        response = self.client.beta.messages.create(
            model=self.model,
            max_tokens=2048,
            output_config={"effort": "low"},  # short answer, keep latency down for voice
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",  # re-run on a fallback model if a safety classifier declines
            messages=[{
                "role": "user",
                "content": [
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}},
                    {"type": "text", "text": PROMPT},
                ],
            }],
        )
        if response.stop_reason == "refusal":
            return "Claude declined to describe this region."
        text = " ".join(b.text for b in response.content if b.type == "text").strip()
        return text or "I couldn't describe that region."

    def speak(self, text):
        if self._say and self._say.poll() is None:
            self._say.terminate()  # a newer snap interrupts the old description
        cmd = ["say"] + (["-v", self.voice] if self.voice else []) + [text]
        self._say = subprocess.Popen(cmd)
