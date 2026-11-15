"""Smallest possible Claude call: proves the key + SDK + model ID work.

Run: python experiments/03_hello_claude.py
"""

import anthropic
from dotenv import load_dotenv

load_dotenv()
client = anthropic.Anthropic()  # reads ANTHROPIC_API_KEY from the environment

response = client.messages.create(
    model="claude-opus-5-5",
    max_tokens=1000,
    output_config={"effort": "low"},  # trivial task -> least thinking
    messages=[{"role": "user", "content": "In one sentence: what is a CAD system in public safety?"}],
)
print(next(b.text for b in response.content if b.type == "text"))
print(f"\n[{response.model}] in={response.usage.input_tokens} out={response.usage.output_tokens} "
      f"stop={response.stop_reason}")
