// LLM client config for talent-engine.
//
// Four providers are wired:
//   - Gemini  (via Vertex AI; fallback to google-ai)  — original / dev default
//   - Qwen    (via DashScope OpenAI-compatible endpoint)
//   - Hunyuan (via Tencent OpenAI-compatible endpoint)
//   - DeepSeek
//
// Functions in parse.baml + match.baml don't pin a specific provider.
// They reference the `Default` client below, which is itself selected
// at call time via `baml_options={"client": <name>}` from Python — driven
// by the `LLM_PROVIDER` env var in app/runtime config.
//
// Why this shape: at deploy time the same image runs against any
// provider depending on environment, no rebuild needed. Eval-mode
// scripts can also iterate provider-by-provider on the same code.

generator target {
  output_type python/pydantic
  output_dir "../"
  version "0.220.0"
}

// ---------------------------------------------------------------------------
// Default client — overridden per-call from Python via baml_options.client.
// Kept as a name reference so .baml files stay provider-agnostic.
// ---------------------------------------------------------------------------

client<llm> Default {
  provider fallback
  options {
    strategy [Gemini]
  }
}

// ---------------------------------------------------------------------------
// Gemini — original. Vertex preferred (auth via ADC), AI Studio as fallback.
// ---------------------------------------------------------------------------

client<llm> Gemini {
  provider fallback
  options {
    strategy [GeminiVertex, GeminiDirect]
  }
}

client<llm> GeminiVertex {
  provider vertex-ai
  options {
    model gemini-2.5-flash
    project_id env.GOOGLE_CLOUD_PROJECT
    location us-central1
    generationConfig {
      maxOutputTokens 65536
    }
  }
}

client<llm> GeminiDirect {
  provider google-ai
  options {
    model gemini-2.5-flash
    api_key env.GEMINI_API_KEY
    generationConfig {
      maxOutputTokens 65536
    }
  }
}

// ---------------------------------------------------------------------------
// Qwen via Aliyun DashScope (OpenAI-compatible).
// Endpoint: https://help.aliyun.com/zh/dashscope/developer-reference/compatibility-of-openai-with-dashscope
// Model selection: qwen-plus is a balanced default; bump to qwen-max for
// the highest-quality runs at ~3× cost.
// ---------------------------------------------------------------------------

client<llm> Qwen {
  provider openai-generic
  options {
    base_url "https://dashscope.aliyuncs.com/compatible-mode/v1"
    api_key env.QWEN_API_KEY
    model "qwen-plus"
  }
}

// ---------------------------------------------------------------------------
// Hunyuan via Tencent's OpenAI-compatible gateway.
// TODO: verify the exact base_url against current Tencent docs — Tencent
// has moved this URL at least once. As of late 2025 the endpoint is
// documented as below; if the call returns 404, double-check Tencent's
// 混元大模型 / OpenAI-兼容接口 docs.
// ---------------------------------------------------------------------------

client<llm> Hunyuan {
  provider openai-generic
  options {
    base_url "https://api.hunyuan.cloud.tencent.com/v1"
    api_key env.HUNYUAN_API_KEY
    model "hunyuan-turbo"
  }
}

// ---------------------------------------------------------------------------
// DeepSeek — text-only. Strong Chinese, very low cost per token.
// ---------------------------------------------------------------------------

// deepseek-chat defaults to a 4096 output cap where the Gemini clients set
// 65536. Raised to the model ceiling as a guard: output that exceeds the cap
// truncates mid-JSON and surfaces as a BAML parse error, which
// with_timeout_retry deliberately does not retry.
// Not a demonstrated failure — a 12KB resume parsed fine under both 4096 and
// 8192, because the model compresses `raw_text` rather than echoing it
// verbatim, so output does not grow linearly with input.
client<llm> DeepSeek {
  provider openai-generic
  options {
    base_url "https://api.deepseek.com/v1"
    api_key env.DEEPSEEK_API_KEY
    model "deepseek-chat"
    max_tokens 8192
  }
}

