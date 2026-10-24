#!/usr/bin/env python3
"""Сверка остатков Wildberries (FBS) с МойСкладом.

По умолчанию только читает: показывает, где на складах продавца в WB остаток БОЛЬШЕ,
чем «Доступно» на складе МойСклада. С флагом --apply снижает остаток WB до значения
МойСклада — только вниз и только для артикулов, найденных в МойСкладе. Перед любой
записью сохраняется снимок «как было» (CSV), из него можно откатить.

  export MOYSKLAD_TOKEN=...            # токен JSON API 1.2
  export WB_TOKEN_SHOP1=...            # токен WB с правом «Маркетплейс» и «Контент»
  python3 stock_sync.py --wb-token-env WB_TOKEN_SHOP1
  python3 stock_sync.py --wb-token-env WB_TOKEN_SHOP1 --wb-token-env WB_TOKEN_SHOP2 --store "Основной склад"
  python3 stock_sync.py --wb-token-env WB_TOKEN_SHOP1 --apply --exclude-warehouse 123456
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import gzip
import json
import os
import pathlib
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

MS_API = 'https://api.moysklad.ru/api/remap/1.2'
WB_CONTENT = 'https://content-api.wildberries.ru/content/v2/get/cards/list'
WB_MARKET = 'https://marketplace-api.wildberries.ru/api/v3'


# ---------- HTTP ----------
def http(method: str, url: str, headers: dict, body=None, tries: int = 5):
    """JSON-запрос с повтором на 429 и разжатием gzip. Возвращает (код, данные или текст ошибки)."""
    data = json.dumps(body).encode() if body is not None else None
    for i in range(tries):
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
                if r.headers.get('Content-Encoding') == 'gzip':
                    raw = gzip.decompress(raw)
                txt = raw.decode()
                return r.status, (json.loads(txt) if txt.strip() else {})
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(20 * (i + 1))
                continue
            return e.code, e.read().decode(errors='replace')[:300]
    return 429, 'rate limit'


def ms_get(path: str, token: str):
    code, d = http('GET', f'{MS_API}/{path}', {'Authorization': f'Bearer {token}', 'Accept-Encoding': 'gzip'})
    if code != 200:
        raise SystemExit(f'МойСклад: HTTP {code} {d}')
    return d


def wb(method: str, url: str, token: str, body=None):
    return http(method, url, {'Authorization': token, 'Content-Type': 'application/json'}, body)


# ---------- данные ----------
def ms_stock(token: str, store: str) -> dict[str, int]:
    """Артикул → доступный остаток на складе МойСклада."""
    rows = ms_get('entity/store?filter=name=' + urllib.parse.quote(store), token)['rows']
    if not rows:
        raise SystemExit(f'МойСклад: склад «{store}» не найден')
    href = rows[0]['meta']['href']
    res, off = {}, 0
    while True:
        page = ms_get(f'entity/assortment?filter=stockStore={href}&limit=1000&offset={off}', token)
        for r in page.get('rows', []):
            art = (r.get('article') or r.get('code') or '').strip()
            if art:
                res[art] = int(r.get('quantity', r.get('stock', 0)) or 0)
        if len(page.get('rows', [])) < 1000:
            return res
        off += 1000
        time.sleep(0.5)


def wb_sku_to_article(token: str) -> dict[str, str]:
    """Баркод (sku) → артикул продавца (vendorCode) по всем карточкам."""
    out, cursor = {}, {'limit': 100}
    while True:
        code, d = wb('POST', WB_CONTENT, token, {'settings': {'cursor': cursor, 'filter': {'withPhoto': -1}}})
        if code != 200:
            raise SystemExit(f'WB контент: HTTP {code} {d}')
        for card in d.get('cards', []):
            for size in card.get('sizes', []):
                for sku in size.get('skus', []):
                    out[sku] = card.get('vendorCode', '')
        c = d.get('cursor', {})
        if c.get('total', 0) < 100:
            return out
        cursor = {'limit': 100, 'updatedAt': c['updatedAt'], 'nmID': c['nmID']}
        time.sleep(0.7)


def wb_stock_rows(token: str, sku2art: dict[str, str], ms: dict[str, int], exclude: set[int]) -> list[dict]:
    code, whs = wb('GET', f'{WB_MARKET}/warehouses', token)
    if code != 200:
        raise SystemExit(f'WB склады: HTTP {code} {whs}')
    skus, rows = list(sku2art), []
    for w in whs:
        if w['id'] in exclude:
            continue
        for i in range(0, len(skus), 1000):
            code, d = wb('POST', f"{WB_MARKET}/stocks/{w['id']}", token, {'skus': skus[i:i + 1000]})
            if code != 200:
                raise SystemExit(f"WB склад {w['id']}: HTTP {code} {d}")
            for s in d.get('stocks', []):
                art = sku2art.get(s['sku'], '')
                rows.append({'warehouse': w['id'], 'wh_name': w.get('name', ''), 'sku': s['sku'],
                             'article': art, 'wb': s['amount'], 'ms': ms.get(art)})
            time.sleep(0.3)
    return rows


# ---------- логика (без сети, покрыта тестами) ----------
def overstock(rows: list[dict]) -> list[dict]:
    """Строки, где на WB больше, чем есть в МойСкладе. Артикулы, которых нет в МС, не трогаем."""
    return [r for r in rows if r['ms'] is not None and r['wb'] > max(r['ms'], 0)]


def unknown(rows: list[dict]) -> list[dict]:
    return [r for r in rows if r['ms'] is None and r['wb'] > 0]


def fix_plan(rows: list[dict]) -> dict[int, list[dict]]:
    """Склад WB → список {sku, amount} для записи. Только вниз, не ниже нуля."""
    plan: dict[int, list[dict]] = {}
    for r in overstock(rows):
        plan.setdefault(r['warehouse'], []).append({'sku': r['sku'], 'amount': max(r['ms'], 0)})
    return plan


# ---------- запуск ----------
def run(name: str, token: str, ms: dict[str, int], args, snapdir: pathlib.Path) -> None:
    print(f'== {name}')
    rows = wb_stock_rows(token, wb_sku_to_article(token), ms, set(args.exclude_warehouse))
    stamp = dt.datetime.now().strftime('%Y%m%d-%H%M%S')
    snap = snapdir / f'{name}-{stamp}.csv'
    with snap.open('w', newline='', encoding='utf-8') as f:
        w = csv.DictWriter(f, fieldnames=['warehouse', 'wh_name', 'sku', 'article', 'wb', 'ms'])
        w.writeheader()
        w.writerows(rows)
    over, unk = overstock(rows), unknown(rows)
    print(f'  с остатком на WB: {sum(1 for r in rows if r["wb"] > 0)} · WB больше МС: {len(over)} · '
          f'нет в МС (не трогаем): {len(unk)} · снимок: {snap}')
    for r in sorted(over, key=lambda r: r['wb'] - max(r['ms'], 0), reverse=True)[:15]:
        print(f'    {r["article"]}: WB {r["wb"]} · МС {r["ms"]} · склад {r["wh_name"] or r["warehouse"]}')
    if not args.apply:
        return
    for wid, stocks in fix_plan(rows).items():
        for i in range(0, len(stocks), 1000):
            code, d = wb('PUT', f'{WB_MARKET}/stocks/{wid}', token, {'stocks': stocks[i:i + 1000]})
            ok = code in (200, 204)
            print(f'  запись, склад {wid}: {len(stocks[i:i + 1000])} шт → HTTP {code}{"" if ok else " " + str(d)}')
            time.sleep(1)
    print(f'  откат: значения столбца «wb» из {snap}')


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description='Сверка остатков Wildberries FBS с МойСкладом')
    p.add_argument('--wb-token-env', action='append', required=True,
                   help='имя переменной окружения с токеном WB (можно несколько — по кабинету)')
    p.add_argument('--ms-token-env', default='MOYSKLAD_TOKEN', help='переменная с токеном МойСклада')
    p.add_argument('--store', default='Основной склад', help='склад МойСклада для сравнения')
    p.add_argument('--exclude-warehouse', type=int, action='append', default=[],
                   help='ID склада WB, который не сверять (например, со стоками поставщика)')
    p.add_argument('--apply', action='store_true', help='снизить остатки WB до МойСклада')
    p.add_argument('--snapshots', default='snapshots', help='папка для снимков «как было»')
    args = p.parse_args(argv)

    ms_token = os.environ.get(args.ms_token_env)
    if not ms_token:
        print(f'нет переменной {args.ms_token_env}')
        return 2
    snapdir = pathlib.Path(args.snapshots)
    snapdir.mkdir(parents=True, exist_ok=True)
    print(f'МойСклад: остатки склада «{args.store}»…')
    ms = ms_stock(ms_token, args.store)
    print(f'МойСклад: артикулов {len(ms)}, с остатком: {sum(1 for v in ms.values() if v > 0)}')
    for env in args.wb_token_env:
        token = os.environ.get(env)
        if not token:
            print(f'== {env}: переменная не задана, пропускаю')
            continue
        try:
            run(env, token, ms, args, snapdir)
        except SystemExit as e:
            print(f'  {e}')
    print('режим: запись' if args.apply else 'режим: только чтение (исправить: --apply)')
    return 0


if __name__ == '__main__':
    sys.exit(main())
