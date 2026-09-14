import os
from dotenv import load_dotenv
load_dotenv()

LLM_BACKEND = os.environ.get('LLM_BACKEND', 'ollama')


def llm_chat(messages, model=None):
    """Unified LLM chat interface. Uses local Ollama by default (for
    development on this machine, where a GPU is available). Uses Groq's
    free API instead when LLM_BACKEND=groq is set -- this is for the
    public deployment, which has no local GPU to run Ollama on."""
    if LLM_BACKEND == 'groq':
        return _groq_chat(messages)
    return _ollama_chat(messages, model)


def _ollama_chat(messages, model):
    import ollama
    model = model or 'llama3.2:3b'
    response = ollama.chat(model=model, messages=messages)
    return response['message']['content']


def _groq_chat(messages):
    from groq import Groq
    api_key = os.environ.get('GROQ_API_KEY')
    if not api_key:
        raise RuntimeError(
            "LLM_BACKEND=groq is set but GROQ_API_KEY is missing from "
            "the environment -- check your .env file."
        )
    client = Groq(api_key=api_key)
    response = client.chat.completions.create(
        # openai/gpt-oss-20b is Groq's current recommended replacement for
        # llama-3.1-8b-instant, which they deprecated for free/developer
        # tier usage on 2026-06-17. Check console.groq.com/docs/deprecations
        # if this ever needs revisiting.
        model='openai/gpt-oss-20b',
        messages=messages,
    )
    return response.choices[0].message.content
