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
    python3 comext_ethanol_agent.py --year 2026 --end 10 --partner ALL

With --partner ALL every partner country is pulled, with imports, exports and
net imports (imports minus exports) and their share of consumption, over the
months for which both trade and consumption data exist. Partners are grouped
into EU and non-EU because the two have different customs status: EU ethanol
is Union goods (T2), while non-EU ethanol is non-Union (T1) until released for
free circulation, and T1 and T2 product cannot share a tank.

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

# Density of ethanol, t/m3 (= kg/l). Used to turn the energy
# statistics (thousand tonnes) into volume, and as a fallback for trade rows
# that have a net mass but no supplementary quantity.
ETHANOL_DENSITY = 0.7887

EU27 = frozenset("AT BE BG HR CY CZ DK EE FI FR DE GR HU IE IT LV LT LU MT NL "
                 "PL PT RO SK SI ES SE".split())

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
    if partner.upper() == "ALL":
        partner = ""  # empty SDMX key position = every partner
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


def fetch_partner_names() -> dict[str, str]:
    """Comext partner code -> English name."""
    url = (f"{COMEXT_API.rsplit('/', 1)[0]}/codelist/ESTAT/CXT_FREE_ISO"
           "?format=TSV&lang=en")
    names = {}
    for line in http_get(url).decode("utf-8-sig").splitlines():
        code, _, name = line.partition("\t")
        # Drop historical footnotes such as "(incl. ... from 1995 -> 2000)".
        names[code] = name.split(" (")[0].strip()
    return names


def to_m3(t: dict[str, float]) -> tuple[float | None, float | None, float, str]:
    """(kg, litres, m3, basis) for one partner-month of Comext indicators."""
    kg = t["QUANTITY_IN_100KG"] * 100 if "QUANTITY_IN_100KG" in t else None
    litres = t.get("SUPPLEMENTARY_QUANTITY")
    if litres:
        return kg, litres, litres / 1000, "supplementary quantity (litres)"
    if kg:
        return kg, litres, kg / ETHANOL_DENSITY / 1000, "net mass / density"
    return kg, litres, 0.0, "no trade reported"


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
        kg, litres, m3, basis = to_m3(t)
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


def build_partner_report(args: argparse.Namespace) -> dict:
    """All partner countries: imports, exports and net, as share of consumption."""
    months = periods(args.year, args.start, args.end)
    rows, comext_url = fetch_comext(args.reporter, "ALL", args.product,
                                    "1+2", months)
    cons, energy_url = fetch_consumption(args.reporter, months, args.siec,
                                         args.nrg_bal)
    names = fetch_partner_names()

    # trade[flow][partner][month][indicator]; flow "1" = import, "2" = export
    trade: dict[str, dict[str, dict[str, dict[str, float]]]] = {"1": {}, "2": {}}
    for row in rows:
        if row["OBS_VALUE"] not in ("", None):
            trade[row["flow"]].setdefault(row["partner"], {}).setdefault(
                row["TIME_PERIOD"], {})[row["indicators"]] = float(row["OBS_VALUE"])

    # Intra-EU trade is published later than extra-EU trade, so a month only
    # counts as complete once the WORLD aggregate has a row for it.
    window = [m for m in months if m in cons
              and all(m in trade[f].get("WORLD", {}) for f in ("1", "2"))]
    cons_m3 = sum(cons[m] for m in window) * 1000 / ETHANOL_DENSITY

    def share(m3: float) -> float | None:
        return m3 / cons_m3 if cons_m3 else None

    def summarise(code: str) -> dict:
        out = {"partner": code, "name": names.get(code, code),
               "group": "EU" if code in EU27 else "non-EU"}
        for flow, label in (("1", "import"), ("2", "export")):
            by_month = trade[flow].get(code, {})
            monthly = {m: to_m3(by_month.get(m, {}))[2] for m in months}
            out[f"{label}_m3"] = sum(monthly[m] for m in window)
            out[f"{label}_value_eur"] = sum(
                by_month.get(m, {}).get("VALUE_IN_EUROS", 0) for m in window)
            out[f"{label}_monthly_m3"] = monthly
        out["net_m3"] = out["import_m3"] - out["export_m3"]
        out["share"] = share(out["import_m3"])
        out["net_share"] = share(out["net_m3"])
        return out

    def subtotal(group: str, members: list[dict]) -> dict:
        tot = {"partner": "", "name": f"{group} subtotal", "group": group}
        for key in ("import_m3", "export_m3", "net_m3"):
            tot[key] = sum(p[key] for p in members)
        tot["share"], tot["net_share"] = share(tot["import_m3"]), share(tot["net_m3"])
        return tot

    # Aggregates (WORLD, EXT_EU, INT_EU27_2020, ...) have codes longer than
    # two characters; individual partners are two-letter codes.
    codes = {c for f in trade.values() for c in f if len(c) == 2}
    partners = sorted((summarise(c) for c in codes),
                      key=lambda p: -p["import_m3"])
    groups = [subtotal(g, [p for p in partners if p["group"] == g])
              for g in ("EU", "non-EU")]
    return {
        "query": {
            "reporter": args.reporter, "partner": "ALL",
            "product": args.product, "flow": "1+2",
            "period_requested": f"{months[0]}..{months[-1]}",
            "period_compared": f"{window[0]}..{window[-1]}" if window else None,
            "months_excluded": [m for m in months if m not in window],
            "consumption_series": f"{ENERGY_DATASET} {args.siec} {args.nrg_bal}",
            "density_t_per_m3": ETHANOL_DENSITY,
        },
        "sources": {"comext": comext_url, "consumption": energy_url},
        "comext_last_update": rows[0]["LAST UPDATE"] if rows else None,
        "consumption_m3": cons_m3,
        "partners": partners,
        "groups": groups,
        "world": {**summarise("WORLD"), "name": "all partners (WORLD)", "group": ""},
    }


def print_partner_report(rep: dict, top: int) -> None:
    q = rep["query"]
    print(f"Comext {COMEXT_DATASET}: reporter {q['reporter']}, all partners, "
          f"CN {q['product']}, imports and exports")
    print(f"Comext last update: {rep['comext_last_update']}")
    if not q["period_compared"]:
        print(f"\nNo month in {q['period_requested']} has both trade and "
              "consumption data.")
        return
    print(f"Compared period: {q['period_compared']}"
          + (f"  (excluded, data incomplete: {', '.join(q['months_excluded'])})"
             if q["months_excluded"] else ""))
    print(f"Consumption ({q['consumption_series']}, {q['density_t_per_m3']} t/m3): "
          f"{rep['consumption_m3']:,.0f} m3")
    print("Shares are of consumption; net = imports - exports.\n")

    head = (f"{'#':>3} {'':2} {'partner':24} {'':6} {'imports m3':>11} "
            f"{'exports m3':>11} {'net m3':>11} {'imp.share':>9} {'net share':>9}")
    print(head)
    print("-" * len(head))

    def line(rank: str, p: dict) -> None:
        print(f"{rank:>3} {p['partner']:2} {p['name'][:24]:24} {p['group']:6} "
              f"{p['import_m3']:>11,.0f} {p['export_m3']:>11,.0f} "
              f"{p['net_m3']:>11,.0f} {fmt(p['share'], '.1%'):>9} "
              f"{fmt(p['net_share'], '.1%'):>9}")

    partners = [p for p in rep["partners"]
                if p["import_m3"] >= 0.5 or p["export_m3"] >= 0.5]
    shown = partners[:top] if top else partners
    for i, p in enumerate(shown, 1):
        line(str(i), p)
    rest = partners[len(shown):]
    if rest:
        other = {"partner": "", "name": f"other ({len(rest)} countries)", "group": ""}
        for key in ("import_m3", "export_m3", "net_m3", "share", "net_share"):
            other[key] = sum(p[key] for p in rest)
        line("", other)
    print("-" * len(head))
    for g in rep["groups"]:
        line("", {**g, "group": ""})
    line("", {**rep["world"], "partner": ""})


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
    p.add_argument("--partner", default="US",
                   help="partner code, or ALL for every origin country")
    p.add_argument("--top", type=int, default=20,
                   help="with --partner ALL: countries to list (0 = all)")
    p.add_argument("--product", default="22071000", help="CN8 code, no spaces")
    p.add_argument("--flow", default="1",
                   help="1 = import, 2 = export (ignored with --partner ALL)")
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
        by_partner = args.partner.upper() == "ALL"
        rep = build_partner_report(args) if by_partner else build_report(args)
    except urllib.error.HTTPError as err:
        print(f"Eurostat rejected the request (HTTP {err.code}) - check the "
              f"reporter/partner/product codes.\n{err.url}", file=sys.stderr)
        return 1
    if by_partner:
        print_partner_report(rep, args.top)
    else:
        print_report(rep)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as fh:
            json.dump(rep, fh, indent=2)
        print(f"\nWrote {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
