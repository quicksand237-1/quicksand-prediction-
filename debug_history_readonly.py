"""Explicit local-only debug server/probe. Never constructs a writable Store."""
import argparse
import json
import sqlite3
import threading
import time
from pathlib import Path
from http.server import ThreadingHTTPServer
from live_dashboard_server import LiveService, DashboardHandler
from polymarket_pipeline import MarketStore, load_model


def make_service(db="data/extended.sqlite", model="data/extended_model_v4.json"):
    service = LiveService.__new__(LiveService)
    service.args = argparse.Namespace(db=db, timezone="America/New_York", user="elonmusk", platform="X",
                                      paper_fee_rate=.02, paper_slippage_bps=50., paper_size=1.)
    service.lock = threading.RLock()
    service.store = MarketStore.__new__(MarketStore)
    service.store.conn = sqlite3.connect(Path(db).resolve().as_uri()+"?mode=ro", uri=True, check_same_thread=False)
    service.model = load_model(Path(model))
    service.model_payload = json.loads(Path(model).read_text(encoding="utf-8"))
    return service


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--serve", type=int)
    parser.add_argument("--slug", action="append")
    parser.add_argument("--limit", type=int, default=10000)
    args = parser.parse_args()
    service = make_service()
    if args.serve is not None:
        class Handler(DashboardHandler):
            static_file = Path("forecast_dashboard.html")

            def do_GET(self):
                if self.path.split("?")[0] in {"/", "/forecast_dashboard.html", "/api/local-markets", "/api/backtest-review"} or self.path.startswith("/api/backtest-jobs/"):
                    return super().do_GET()
                self._json_response(200, {"ok": True, "markets": [], "state": "disabled_readonly_debug"})

            def do_POST(self):
                self._json_response(403, {"ok": False, "message": "只读调试禁用写入和外部接口"})

        Handler.service = service
        server = ThreadingHTTPServer(("127.0.0.1", args.serve), Handler)
        print(f"READONLY http://127.0.0.1:{server.server_port}", flush=True)
        try:
            server.serve_forever()
        finally:
            server.server_close()
            service.store.conn.close()
    else:
        try:
            for slug in args.slug or []:
                started = time.monotonic()
                result = service.backtest_review(slug=slug, limit=args.limit)
                records = result["records"]
                print(json.dumps(dict(slug=slug, seconds=round(time.monotonic()-started, 3), records=len(records),
                    first=records[0]["probability_guard"] if records else None,
                    safe_checkpoints=sum(any(r["point_in_time_safe"] for r in x["outcome_market_rows"]) for x in records),
                    fills=sum(bool(a["quantity_delta"]) for x in records for a in x["position_actions"]),
                    entry_coverage=records[-1]["paper_entry_coverage_so_far"] if records else None)), flush=True)
        finally:
            service.store.conn.close()
