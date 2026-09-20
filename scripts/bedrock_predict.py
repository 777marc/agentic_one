"""Call AWS Bedrock (Claude via Converse API) to produce a directional forecast
for a stock watchlist, optionally grounded by a Bedrock Knowledge Base.

Prerequisites:
    pip install boto3 yfinance
    AWS credentials configured (env vars, ~/.aws/credentials, or --profile)
    Bedrock model access enabled for the chosen model id in your AWS account/region

Example:
    python scripts/bedrock_predict.py --tickers AAPL,NVDA --context notes.txt
    python scripts/bedrock_predict.py --tickers AAPL --kb-id ABCD1234 --region us-east-1
    python scripts/bedrock_predict.py --tickers AAPL --no-price-data
"""
import argparse
import sys

import boto3
import yfinance as yf

DEFAULT_MODEL_ID = "us.anthropic.claude-sonnet-4-5-20250929-v1:0"

# Standard on-demand Claude Sonnet pricing (USD per 1M tokens); update if your model/region differs.
PRICE_PER_1M_INPUT = 3.00
PRICE_PER_1M_OUTPUT = 15.00


SYSTEM_PROMPT = (
    "You are a market research analyst. Given a ticker and supporting context "
    "(news, filings, macro conditions), produce a probabilistic directional "
    "forecast (up/down/flat) with a confidence level, key drivers, and risks. "
    "Never give financial advice or price targets. If context is insufficient, "
    "say so instead of guessing."
)


def fetch_price_data(tickers: list[str], history_period: str = "3mo") -> str:
    """Pull recent price action and key fundamentals per ticker via yfinance."""
    lines = []
    for ticker in tickers:
        try:
            t = yf.Ticker(ticker)
            hist = t.history(period=history_period)
            if hist.empty:
                lines.append(f"{ticker}: no price data available.")
                continue

            last_close = hist["Close"].iloc[-1]
            period_start = hist["Close"].iloc[0]
            pct_change = (last_close - period_start) / period_start * 100
            avg_volume = hist["Volume"].mean()
            high_52w = hist["High"].max()
            low_52w = hist["Low"].min()

            info = t.info or {}
            pe_ratio = info.get("trailingPE")
            market_cap = info.get("marketCap")

            lines.append(
                f"{ticker}: last close={last_close:.2f}, {history_period} change={pct_change:+.1f}%, "
                f"avg volume={avg_volume:,.0f}, {history_period} range={low_52w:.2f}-{high_52w:.2f}, "
                f"P/E={pe_ratio}, market cap={market_cap}"
            )
        except Exception as exc:  # yfinance/network failures shouldn't abort the whole run
            lines.append(f"{ticker}: price data fetch failed ({exc}).")
    return "Price/fundamentals data:\n" + "\n".join(lines)


def retrieve_kb_context(kb_id: str, query: str, region: str, profile: str | None) -> str:
    """Pull grounded context from a Bedrock Knowledge Base via retrieve_and_generate."""
    session = boto3.Session(profile_name=profile, region_name=region)
    client = session.client("bedrock-agent-runtime")
    response = client.retrieve_and_generate(
        input={"text": query},
        retrieveAndGenerateConfiguration={
            "type": "KNOWLEDGE_BASE",
            "knowledgeBaseConfiguration": {
                "knowledgeBaseId": kb_id,
                "modelArn": DEFAULT_MODEL_ID,
            },
        },
    )
    return response["output"]["text"]


def predict(tickers: list[str], context: str, model_id: str, region: str, profile: str | None) -> str:
    session = boto3.Session(profile_name=profile, region_name=region)
    client = session.client("bedrock-runtime")

    user_prompt = (
        f"Watchlist: {', '.join(tickers)}\n\n"
        f"Context (news, filings, macro conditions):\n{context}\n\n"
        "Produce a markdown table with columns: Ticker | Direction | Confidence | "
        "Key Drivers | Risks | Sources. Then add a short Macro/Sector Notes section."
    )

    response = client.converse(
        modelId=model_id,
        system=[{"text": SYSTEM_PROMPT}],
        messages=[{"role": "user", "content": [{"text": user_prompt}]}],
        inferenceConfig={"temperature": 0.3, "maxTokens": 2000},
    )

    usage = response.get("usage", {})
    input_tokens = usage.get("inputTokens", 0)
    output_tokens = usage.get("outputTokens", 0)
    cost = (input_tokens / 1_000_000) * PRICE_PER_1M_INPUT + (output_tokens / 1_000_000) * PRICE_PER_1M_OUTPUT
    print(
        f"[Bedrock usage] input_tokens={input_tokens} output_tokens={output_tokens} "
        f"estimated_cost=${cost:.4f} (at ${PRICE_PER_1M_INPUT}/1M in, ${PRICE_PER_1M_OUTPUT}/1M out)",
        file=sys.stderr,
    )

    return response["output"]["message"]["content"][0]["text"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True, help="Comma-separated tickers, e.g. AAPL,NVDA")
    parser.add_argument("--context", help="Path to a file with research notes, or inline text")
    parser.add_argument("--kb-id", help="Bedrock Knowledge Base id for grounded retrieval")
    parser.add_argument("--model-id", default=DEFAULT_MODEL_ID, help="Bedrock model id")
    parser.add_argument("--region", default="us-east-1", help="AWS region")
    parser.add_argument("--profile", default=None, help="AWS named profile")
    parser.add_argument("--price-history", default="3mo", help="yfinance history period, e.g. 1mo, 3mo, 1y")
    parser.add_argument("--no-price-data", action="store_true", help="Skip the yfinance price/fundamentals fetch")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]

    context = ""
    if args.context:
        try:
            with open(args.context, "r", encoding="utf-8") as f:
                context = f.read()
        except FileNotFoundError:
            context = args.context  # treat as inline text

    if not args.no_price_data:
        price_context = fetch_price_data(tickers, args.price_history)
        context = f"{context}\n\n{price_context}".strip()

    if args.kb_id:
        kb_context = retrieve_kb_context(
            args.kb_id, f"Recent news and conditions for {', '.join(tickers)}", args.region, args.profile
        )
        context = f"{context}\n\n{kb_context}".strip()

    if not context:
        print("No context provided (--context or --kb-id). Aborting.", file=sys.stderr)
        sys.exit(1)

    result = predict(tickers, context, args.model_id, args.region, args.profile)
    print(result)


if __name__ == "__main__":
    main()
