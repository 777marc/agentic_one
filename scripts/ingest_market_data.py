"""Scheduled ingestion of market data snapshots into partitioned Parquet, with
point-in-time timestamps so backtests never use information from the future.

Each row written includes:
    event_time  - when the underlying data actually occurred/was published
    ingested_at - when this job pulled it (UTC, set once per run)

Datasets (all optional, skipped gracefully if a source/key is unavailable):
    prices       - OHLCV history per ticker (yfinance)
    sectors      - OHLCV history for a fixed basket of sector/index ETFs (yfinance)
    rates        - macro/economic series from FRED (requires FRED_API_KEY)
    filings      - recent SEC filings metadata per ticker (SEC EDGAR, free)
    news         - recent headlines per ticker (yfinance .news, free/best-effort)

Storage: writes to a local folder by default (./data/<dataset>/dt=YYYY-MM-DD/...),
or to S3 if --bucket is given (same partitioning under s3://<bucket>/<prefix>/...).

Run this on a schedule (Windows Task Scheduler, cron, or an EventBridge-triggered
Lambda in AWS) rather than having the research agent pull full history on every
request.

Prerequisites:
    pip install -r requirements.txt   (adds pyarrow, requests)
    Set FRED_API_KEY env var to enable the rates dataset (free key: https://fred.stlouisfed.org/docs/api/api_key.html)

Examples:
    python scripts/ingest_market_data.py --tickers AAPL,NVDA,DIS
    python scripts/ingest_market_data.py --tickers AAPL,NVDA --bucket my-bucket --prefix market-data
    python scripts/ingest_market_data.py --tickers AAPL --skip-rates --skip-filings
"""
import argparse
import io
import json
import os
import sys
from datetime import datetime, timezone

import pandas as pd
import requests
import yfinance as yf

SECTOR_ETFS = ["SPY", "QQQ", "DIA", "XLF", "XLK", "XLE", "XLV", "XLY", "XLP", "XLI", "XLB", "XLRE", "XLU", "XLC"]
FRED_SERIES = {"DGS10": "10yr_treasury_yield", "FEDFUNDS": "fed_funds_rate", "CPIAUCSL": "cpi"}
SEC_TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def write_partitioned(df: pd.DataFrame, dataset: str, dt: str, output_dir: str, bucket: str | None, prefix: str) -> None:
    """Write a dataframe as Parquet under dataset/dt=<dt>/, locally and/or to S3."""
    if df.empty:
        print(f"[{dataset}] no rows to write, skipping.", file=sys.stderr)
        return

    buffer = io.BytesIO()
    df.to_parquet(buffer, index=False)
    buffer.seek(0)
    key = f"{dataset}/dt={dt}/{dataset}-{dt}.parquet"

    if bucket:
        import boto3

        s3 = boto3.client("s3")
        s3.put_object(Bucket=bucket, Key=f"{prefix}/{key}", Body=buffer.getvalue())
        print(f"[{dataset}] wrote {len(df)} rows to s3://{bucket}/{prefix}/{key}")
    else:
        path = os.path.join(output_dir, key.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(buffer.getvalue())
        print(f"[{dataset}] wrote {len(df)} rows to {path}")


def fetch_prices(tickers: list[str], period: str, ingested_at: str) -> pd.DataFrame:
    rows = []
    for ticker in tickers:
        try:
            hist = yf.Ticker(ticker).history(period=period)
            hist = hist.reset_index()
            for _, r in hist.iterrows():
                rows.append(
                    {
                        "ticker": ticker,
                        "event_time": r["Date"].isoformat(),
                        "ingested_at": ingested_at,
                        "open": float(r["Open"]),
                        "high": float(r["High"]),
                        "low": float(r["Low"]),
                        "close": float(r["Close"]),
                        "volume": int(r["Volume"]),
                    }
                )
        except Exception as exc:
            print(f"[prices] {ticker} failed: {exc}", file=sys.stderr)
    return pd.DataFrame(rows)


def fetch_sectors(period: str, ingested_at: str) -> pd.DataFrame:
    return fetch_prices(SECTOR_ETFS, period, ingested_at)


def fetch_rates(ingested_at: str) -> pd.DataFrame:
    api_key = os.environ.get("FRED_API_KEY")
    if not api_key:
        print("[rates] FRED_API_KEY not set, skipping.", file=sys.stderr)
        return pd.DataFrame()

    rows = []
    for series_id, label in FRED_SERIES.items():
        try:
            resp = requests.get(
                "https://api.stlouisfed.org/fred/series/observations",
                params={"series_id": series_id, "api_key": api_key, "file_type": "json", "sort_order": "desc", "limit": 30},
                timeout=15,
            )
            resp.raise_for_status()
            for obs in resp.json().get("observations", []):
                if obs["value"] == ".":
                    continue
                rows.append(
                    {
                        "series": label,
                        "event_time": obs["date"],
                        "ingested_at": ingested_at,
                        "value": float(obs["value"]),
                    }
                )
        except Exception as exc:
            print(f"[rates] {series_id} failed: {exc}", file=sys.stderr)
    return pd.DataFrame(rows)


def fetch_filings(tickers: list[str], user_agent: str, ingested_at: str) -> pd.DataFrame:
    headers = {"User-Agent": user_agent}
    try:
        resp = requests.get(SEC_TICKER_MAP_URL, headers=headers, timeout=15)
        resp.raise_for_status()
        ticker_to_cik = {v["ticker"].upper(): str(v["cik_str"]).zfill(10) for v in resp.json().values()}
    except Exception as exc:
        print(f"[filings] could not load SEC ticker map: {exc}", file=sys.stderr)
        return pd.DataFrame()

    rows = []
    for ticker in tickers:
        cik = ticker_to_cik.get(ticker.upper())
        if not cik:
            print(f"[filings] {ticker}: no CIK found, skipping.", file=sys.stderr)
            continue
        try:
            resp = requests.get(f"https://data.sec.gov/submissions/CIK{cik}.json", headers=headers, timeout=15)
            resp.raise_for_status()
            recent = resp.json().get("filings", {}).get("recent", {})
            forms = recent.get("form", [])
            dates = recent.get("filingDate", [])
            accessions = recent.get("accessionNumber", [])
            for form, date, accession in list(zip(forms, dates, accessions))[:20]:
                rows.append(
                    {
                        "ticker": ticker,
                        "event_time": date,
                        "ingested_at": ingested_at,
                        "form_type": form,
                        "accession_number": accession,
                    }
                )
        except Exception as exc:
            print(f"[filings] {ticker} failed: {exc}", file=sys.stderr)
    return pd.DataFrame(rows)


def fetch_news(tickers: list[str], ingested_at: str) -> pd.DataFrame:
    rows = []
    for ticker in tickers:
        try:
            for item in yf.Ticker(ticker).news or []:
                content = item.get("content", item)
                published = content.get("pubDate") or content.get("providerPublishTime")
                rows.append(
                    {
                        "ticker": ticker,
                        "event_time": published,
                        "ingested_at": ingested_at,
                        "title": content.get("title"),
                        "url": (content.get("canonicalUrl") or {}).get("url") or content.get("link"),
                        "publisher": (content.get("provider") or {}).get("displayName"),
                    }
                )
        except Exception as exc:
            print(f"[news] {ticker} failed: {exc}", file=sys.stderr)
    return pd.DataFrame(rows)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tickers", required=True, help="Comma-separated tickers, e.g. AAPL,NVDA")
    parser.add_argument("--period", default="5d", help="yfinance history period per run, e.g. 1d, 5d, 1mo (default 5d)")
    parser.add_argument("--output-dir", default="data", help="Local output root (used when --bucket is not set)")
    parser.add_argument("--bucket", default=None, help="S3 bucket to write to instead of/alongside local disk")
    parser.add_argument("--prefix", default="market-data", help="S3 key prefix (default: market-data)")
    parser.add_argument("--user-agent", default="agentic_one research contact@example.com", help="Required by SEC EDGAR; set to your app name + contact email")
    parser.add_argument("--skip-prices", action="store_true")
    parser.add_argument("--skip-sectors", action="store_true")
    parser.add_argument("--skip-rates", action="store_true")
    parser.add_argument("--skip-filings", action="store_true")
    parser.add_argument("--skip-news", action="store_true")
    args = parser.parse_args()

    tickers = [t.strip().upper() for t in args.tickers.split(",") if t.strip()]
    ingested_at = utcnow_iso()
    dt = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    if not args.skip_prices:
        write_partitioned(fetch_prices(tickers, args.period, ingested_at), "prices", dt, args.output_dir, args.bucket, args.prefix)
    if not args.skip_sectors:
        write_partitioned(fetch_sectors(args.period, ingested_at), "sectors", dt, args.output_dir, args.bucket, args.prefix)
    if not args.skip_rates:
        write_partitioned(fetch_rates(ingested_at), "rates", dt, args.output_dir, args.bucket, args.prefix)
    if not args.skip_filings:
        write_partitioned(fetch_filings(tickers, args.user_agent, ingested_at), "filings", dt, args.output_dir, args.bucket, args.prefix)
    if not args.skip_news:
        write_partitioned(fetch_news(tickers, ingested_at), "news", dt, args.output_dir, args.bucket, args.prefix)


if __name__ == "__main__":
    main()
