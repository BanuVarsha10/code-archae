FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

# CPU-only torch, installed separately from its own index -- the deployed
# container has no GPU, and the default PyPI wheel would pull a much
# larger CUDA-enabled build for no benefit.
RUN pip install --no-cache-dir torch==2.5.1 --index-url https://download.pytorch.org/whl/cpu
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Deployment-specific settings -- LLM_BACKEND=groq since there's no local
# Ollama/GPU here, DISABLE_LIVE_INDEXING=true since this is a public demo
# against a fixed set of pre-indexed repos, KMP_DUPLICATE_LIB_OK=TRUE as a
# defensive measure against the same OpenMP conflict already seen once on
# Windows -- cheap to set even if Linux doesn't hit it.
ENV LLM_BACKEND=groq
ENV DISABLE_LIVE_INDEXING=true
ENV KMP_DUPLICATE_LIB_OK=TRUE

EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
