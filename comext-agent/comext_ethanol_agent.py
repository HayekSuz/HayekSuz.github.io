#!/usr/bin/env python3
"""Comext ethanol agent.

Pulls monthly imports from Eurostat Comext (dataset DS-045409, EU trade by
HS2-4-6 / CN8) for a reporter / partner / CN8 code, converts them to cubic
metres, and compares them with the reporter's fuel ethanol consumption taken
from Eurostat's monthly oil statistics (nrg_cb_oilm, blended biogasoline,
gross inland deliveries observed).

Defaults: reporter NL, partner US, CN 2207 10 00, January-June.

    python3 comext_ethanol_agent.py --year 2026
    python3 comext_ethanol_agent.py --year 2025 --start 1 --end 12 --json out.json

Standard library only.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

COMEXT_API = "https://ec.europa.eu/eurostat/api/comext/dissemination/sdmx/2.1/data"
COMEXT_DATASET = "DS-045409"
ENERGY_API = "https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data"
ENERGY_DATASET = "nrg_cb_oilm"

# Density of ethanol at 20 degC, t/m3 (= kg/l). Used to turn the energy
# statistics (thousand tonnes) into volume, and as a fallback for trade rows
# that have a net mass but no supplementary quantity.
ETHANOL_DENSITY = 0.789

INDICATORS = ("QUANTITY_IN_100KG", "SUPPLEMENTARY_QUANTITY", "VALUE_IN_EUROS")


def http_get(url: str, retries: int = 4) -> bytes:
    delay = 2.0
    for attempt in range(retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "comext-ethanol-agent"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                return resp.read()
        except urllib.error.HTTPError as err:
            # 4xx other than 429 will not get better on retry.
            if err.code < 500 and err.code != 429:
                raise
            last = err
        except (urllib.error.URLError, TimeoutError) as err:
            last = err
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"GET failed after {retries + 1} attempts: {url}: {last}")


def periods(year: int, start: int, end: int) -> list[str]:
    return [f"{year}-{m:02d}" for m in range(start, end + 1)]


def fetch_comext(reporter: str, partner: str, product: str, flow: str,
                 months: list[str]) -> tuple[list[dict], str]:
    """Return the raw Comext rows (one per indicator per month) and the URL."""
    key = f"M.{reporter}.{partner}.{product}.{flow}.{'+'.join(INDICATORS)}"
    query = urllib.parse.urlencode(
        {"startPeriod": months[0], "endPeriod": months[-1], "format": "SDMX-CSV"})
    url = f"{COMEXT_API}/{COMEXT_DATASET}/{key}?{query}"
    try:
        text = http_get(url).decode("utf-8-sig")
    except urllib.error.HTTPError as err:
        if err.code == 404:  # Comext answers 404 when the selection is empty
            return [], url
        raise
    if not text.lstrip().startswith("DATAFLOW"):
        raise RuntimeError(f"Unexpected Comext response: {text[:300]}")
    return list(csv.DictReader(io.StringIO(text))), url


def fetch_consumption(geo: str, months: list[str], siec: str,
                      nrg_bal: str) -> tuple[dict[str, float], str]:
    """Monthly deliveries in thousand tonnes, keyed by YYYY-MM."""
    query = urllib.parse.urlencode({
        "format": "JSON", "freq": "M", "geo": geo, "siec": siec,
        "nrg_bal": nrg_bal, "unit": "THS_T",
        "sinceTimePeriod": months[0], "untilTimePeriod": months[-1],
    })
    url = f"{ENERGY_API}/{ENERGY_DATASET}?{query}"
    data = json.loads(http_get(url))
    if "value" not in data:
        raise RuntimeError(f"Unexpected Eurostat response: {str(data)[:300]}")
    # Every dimension but time is fixed to one value, so the flat index is
    # the time index.
    time_index = data["dimension"]["time"]["category"]["index"]
    by_pos = {pos: period for period, pos in time_index.items()}
    return {by_pos[int(i)]: float(v) for i, v in data["value"].items()
            if v is not None}, url


def build_report(args: argparse.Namespace) -> dict:
    months = periods(args.year, args.start, args.end)
    rows, comext_url = fetch_comext(args.reporter, args.partner, args.product,
                                    args.flow, months)
    cons, energy_url = fetch_consumption(args.reporter, months, args.siec,
                                         args.nrg_bal)

    trade: dict[str, dict[str, float]] = {m: {} for m in months}
    for row in rows:
        period = row["TIME_PERIOD"]
        if period in trade and row["OBS_VALUE"] not in ("", None):
            trade[period][row["indicators"]] = float(row["OBS_VALUE"])

    monthly = []
    for m in months:
        t = trade[m]
        kg = t["QUANTITY_IN_100KG"] * 100 if "QUANTITY_IN_100KG" in t else None
        litres = t.get("SUPPLEMENTARY_QUANTITY")
        if litres:
            m3, basis = litres / 1000, "supplementary quantity (litres)"
        elif kg:
            m3, basis = kg / ETHANOL_DENSITY / 1000, "net mass / density"
        else:
            m3, basis = 0.0, "no trade reported"
        kt = cons.get(m)
        cons_m3 = kt * 1000 / ETHANOL_DENSITY if kt is not None else None
        monthly.append({
            "period": m,
            "import_kg": kg,
            "import_litres": litres,
            "import_value_eur": t.get("VALUE_IN_EUROS"),
            "import_m3": m3,
            "import_m3_basis": basis,
            "consumption_kt": kt,
            "consumption_m3": cons_m3,
            "share": m3 / cons_m3 if cons_m3 else None,
        })

    missing = [r["period"] for r in monthly if r["consumption_m3"] is None]
    total_m3 = sum(r["import_m3"] for r in monthly)
    total_cons = sum(r["consumption_m3"] or 0 for r in monthly)
    return {
        "query": {
            "reporter": args.reporter, "partner": args.partner,
            "product": args.product, "flow": args.flow,
            "period": f"{months[0]}..{months[-1]}",
            "consumption_series": f"{ENERGY_DATASET} {args.siec} {args.nrg_bal}",
            "density_t_per_m3": ETHANOL_DENSITY,
        },
        "sources": {"comext": comext_url, "consumption": energy_url},
        "comext_last_update": rows[0]["LAST UPDATE"] if rows else None,
        "comext_rows": rows,
        "monthly": monthly,
        "total": {
            "import_m3": total_m3,
            "import_value_eur": sum(r["import_value_eur"] or 0 for r in monthly),
            "consumption_m3": total_cons,
            # Only meaningful when every month has a consumption figure.
            "share": total_m3 / total_cons if total_cons and not missing else None,
            "months_missing_consumption": missing,
        },
    }


def fmt(value, spec: str = ",.0f") -> str:
    return "-" if value is None else format(value, spec)


def print_report(rep: dict) -> None:
    q = rep["query"]
    print(f"Comext {COMEXT_DATASET}: reporter {q['reporter']}, partner {q['partner']}, "
          f"CN {q['product']}, flow {q['flow']} (1 = import), {q['period']}")
    print(f"Comext last update: {rep['comext_last_update']}\n")

    print("Raw Comext rows")
    print(f"{'period':8} {'indicator':24} {'value':>16}")
    for row in rep["comext_rows"]:
        print(f"{row['TIME_PERIOD']:8} {row['indicators']:24} "
              f"{float(row['OBS_VALUE']):>16,.2f}")
    if not rep["comext_rows"]:
        print("(no rows returned)")

    print(f"\nConversion and share  (consumption = {q['consumption_series']}, "
          f"{q['density_t_per_m3']} t/m3)")
    head = (f"{'period':8} {'imports kg':>14} {'imports l':>14} {'imports m3':>12} "
            f"{'EUR':>13} {'cons. kt':>9} {'cons. m3':>11} {'share':>7}")
    print(head)
    print("-" * len(head))
    for r in rep["monthly"]:
        print(f"{r['period']:8} {fmt(r['import_kg']):>14} {fmt(r['import_litres']):>14} "
              f"{fmt(r['import_m3']):>12} {fmt(r['import_value_eur']):>13} "
              f"{fmt(r['consumption_kt'], ',.3f'):>9} {fmt(r['consumption_m3']):>11} "
              f"{fmt(r['share'], '.1%'):>7}")
    t = rep["total"]
    print("-" * len(head))
    print(f"{'total':8} {'':>14} {'':>14} {fmt(t['import_m3']):>12} "
          f"{fmt(t['import_value_eur']):>13} {'':>9} {fmt(t['consumption_m3']):>11} "
          f"{fmt(t['share'], '.1%'):>7}")
    if t["months_missing_consumption"]:
        print("\nNo consumption data yet for: "
              + ", ".join(t["months_missing_consumption"])
              + " - total share not computed.")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[1],
                                formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    p.add_argument("--year", type=int, required=True)
    p.add_argument("--start", type=int, default=1, help="first month")
    p.add_argument("--end", type=int, default=6, help="last month")
    p.add_argument("--reporter", default="NL")
    p.add_argument("--partner", default="US")
    p.add_argument("--product", default="22071000", help="CN8 code, no spaces")
    p.add_argument("--flow", default="1", help="1 = import, 2 = export")
    p.add_argument("--siec", default="R5210B",
                   help="energy product for consumption (blended biogasoline)")
    p.add_argument("--nrg-bal", default="GID_OBS",
                   help="energy balance item (gross inland deliveries, observed)")
    p.add_argument("--json", metavar="PATH", help="also write the full result as JSON")
    args = p.parse_args(argv)
    if not 1 <= args.start <= args.end <= 12:
        p.error("--start/--end must satisfy 1 <= start <= end <= 12")
    args.product = args.product.replace(" ", "")

    try:
        rep = build_report(args)
    except urllib.error.HTTPError as err:
        print(f"Eurostat rejected the request (HTTP {err.code}) - check the "
              f"reporter/partner/product codes.\n{err.url}", file=sys.stderr)
        return 1
    print_report(rep)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
