# agentic_one

Stock research/forecast tooling powered by AWS Bedrock (Claude), with price/fundamentals data from `yfinance`.

## Contents

- [`scripts/bedrock_predict.py`](scripts/bedrock_predict.py) — CLI that fetches price/fundamentals for a ticker watchlist, optionally merges in news context and Bedrock Knowledge Base retrieval, then calls Bedrock's Converse API (Claude) to produce a probabilistic directional forecast (up/down/flat) with confidence, drivers, risks, and sources.
- [`.github/agents/stock-research.agent.md`](.github/agents/stock-research.agent.md) — a VS Code custom agent ("Stock Research Analyst") that orchestrates web research + this script into a structured report.

## Setup

```powershell
py -3 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

AWS credentials must be configured (env vars, `~/.aws/credentials`, or `--profile`), with Bedrock model access enabled for the chosen model id in your account/region.

## Usage

```powershell
.\.venv\Scripts\python.exe scripts/bedrock_predict.py --tickers AAPL,NVDA
.\.venv\Scripts\python.exe scripts/bedrock_predict.py --tickers AAPL --context notes.txt
.\.venv\Scripts\python.exe scripts/bedrock_predict.py --tickers AAPL --kb-id ABCD1234 --region us-east-1
.\.venv\Scripts\python.exe scripts/bedrock_predict.py --tickers AAPL --no-price-data
```

Key flags:

| Flag                     | Purpose                                                                    |
| ------------------------ | -------------------------------------------------------------------------- |
| `--tickers`              | Comma-separated watchlist (required)                                       |
| `--context`              | Path to a notes file, or inline text, merged into the prompt               |
| `--kb-id`                | Bedrock Knowledge Base id for grounded retrieval                           |
| `--model-id`             | Bedrock model id (default: `us.anthropic.claude-sonnet-4-5-20250929-v1:0`) |
| `--region` / `--profile` | AWS region / named profile                                                 |
| `--price-history`        | `yfinance` history period, e.g. `1mo`, `3mo`, `1y` (default `3mo`)         |
| `--no-price-data`        | Skip the `yfinance` price/fundamentals fetch                               |

Each run prints a `[Bedrock usage]` line to stderr with actual input/output token counts and an estimated cost (based on `PRICE_PER_1M_INPUT`/`PRICE_PER_1M_OUTPUT` in the script — update these to match your model/region's actual Bedrock pricing).

## Notes

- Output is research/analysis only — not financial advice.
- If your chosen model id returns `ResourceNotFoundException` or `ValidationException` about on-demand throughput, check `aws bedrock list-foundation-models` for an `ACTIVE` model in your account and use its cross-region inference profile id (`us.<provider>.<model-id>`).
