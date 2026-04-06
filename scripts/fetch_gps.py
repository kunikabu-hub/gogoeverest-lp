#!/usr/bin/env python3
"""
Garmin MapShare KMLを取得してdata/gps.jsonに保存するスクリプト
GitHub Actionsから30分ごとに実行される
"""

import requests
import json
import re
import os
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

MAPSHARE_URL = "https://share.garmin.com/feed/share/chikaoeverest"
OUTPUT_PATH = "data/gps.json"

# エベレスト南東稜ルートのウェイポイント定義
ROUTE = [
    {"name": "ルクラ出発",       "alt": 2860,  "lat": 27.6868, "lng": 86.7290},
    {"name": "ナムチェバザール",  "alt": 3440,  "lat": 27.8069, "lng": 86.7140},
    {"name": "テンボチェ",       "alt": 3867,  "lat": 27.8360, "lng": 86.7640},
    {"name": "ディンボチェ",     "alt": 4410,  "lat": 27.8990, "lng": 86.8310},
    {"name": "ロブチェ",         "alt": 4940,  "lat": 27.9440, "lng": 86.8120},
    {"name": "ベースキャンプ",   "alt": 5364,  "lat": 27.9881, "lng": 86.8500},
    {"name": "キャンプ1（C1）",  "alt": 5943,  "lat": 27.9960, "lng": 86.8560},
    {"name": "キャンプ2（C2）",  "alt": 6400,  "lat": 27.9990, "lng": 86.8600},
    {"name": "キャンプ3（C3）",  "alt": 7200,  "lat": 28.0020, "lng": 86.8640},
    {"name": "キャンプ4（C4）",  "alt": 7906,  "lat": 28.0050, "lng": 86.8660},
    {"name": "サウスサミット",   "alt": 8749,  "lat": 27.9880, "lng": 86.9240},
    {"name": "エベレスト山頂",   "alt": 8849,  "lat": 27.9881, "lng": 86.9250},
]

def get_data_value(placemark, name):
    """ExtendedDataからname属性に対応するvalueを取得"""
    for data in placemark.findall(".//{http://www.opengis.net/kml/2.2}Data") + placemark.findall(".//Data"):
        if data.get("name") == name:
            val = data.find("{http://www.opengis.net/kml/2.2}value") or data.find("value")
            if val is not None:
                return val.text or ""
    return ""

def get_current_waypoint(alt):
    """現在標高から最も近い通過済みウェイポイントのインデックスを返す"""
    idx = 0
    for i, wp in enumerate(ROUTE):
        if alt >= wp["alt"] - 200:
            idx = i
        else:
            break
    return idx

def fetch_and_convert():
    try:
        print(f"Fetching MapShare KML from {MAPSHARE_URL}...")
        resp = requests.get(MAPSHARE_URL, timeout=30)
        resp.raise_for_status()
        kml_text = resp.text
        print(f"KML fetched: {len(kml_text)} bytes")

        # XMLパース（名前空間を除去して処理）
        kml_clean = re.sub(r' xmlns[^"]*"[^"]*"', '', kml_text)
        root = ET.fromstring(kml_clean)

        # 全Placemarkを取得して最新を選ぶ
        placemarks = root.findall(".//Placemark")
        print(f"Found {len(placemarks)} placemarks")

        latest_pm = None
        latest_time = None

        for pm in placemarks:
            ts = pm.find(".//TimeStamp/when")
            if ts is not None and ts.text:
                try:
                    t = datetime.fromisoformat(ts.text.replace("Z", "+00:00"))
                    if latest_time is None or t > latest_time:
                        latest_time = t
                        latest_pm = pm
                except Exception:
                    pass

        if latest_pm is None:
            raise ValueError("No valid placemark found")

        # データ抽出
        lat_str = get_data_value(latest_pm, "Latitude")
        lng_str = get_data_value(latest_pm, "Longitude")
        elev_str = get_data_value(latest_pm, "Elevation")
        time_str = get_data_value(latest_pm, "Time UTC")
        velocity_str = get_data_value(latest_pm, "Velocity")

        lat = float(lat_str) if lat_str else None
        lng = float(lng_str) if lng_str else None

        # 標高パース "114.25 m from MSL" → 114.25
        alt = None
        if elev_str:
            m = re.search(r"([\d.]+)", elev_str)
            if m:
                alt = float(m.group(1))

        # 速度パース "1.0 km/h"
        velocity = None
        if velocity_str:
            m = re.search(r"([\d.]+)", velocity_str)
            if m:
                velocity = float(m.group(1))

        # 現在地ウェイポイント判定
        cur_idx = get_current_waypoint(alt) if alt else 0

        # 進捗率計算
        base_alt = 2860
        summit_alt = 8849
        pct = 0
        if alt and alt > base_alt:
            pct = min(100, round(((alt - base_alt) / (summit_alt - base_alt)) * 100))

        # 出発前判定（標高が低い場合）
        status = "ASCENDING"
        if alt is not None:
            if alt < 3000:
                status = "PRE-DEPARTURE"
            elif alt >= 8849:
                status = "SUMMIT"
            elif alt >= 7500:
                status = "DEATH ZONE"
            elif alt >= 5364:
                status = "ASCENDING"
            else:
                status = "TREKKING"

        # JSON出力
        output = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_at_jst": datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M JST"),
            "garmin_time": time_str,
            "lat": lat,
            "lng": lng,
            "altitude": alt,
            "velocity_kmh": velocity,
            "current_waypoint_idx": cur_idx,
            "current_waypoint_name": ROUTE[cur_idx]["name"],
            "progress_pct": pct,
            "status": status,
            "route": ROUTE
        }

        os.makedirs("data", exist_ok=True)
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"✅ GPS data saved to {OUTPUT_PATH}")
        print(f"   Alt: {alt}m | Status: {status} | Progress: {pct}%")
        print(f"   Waypoint: {ROUTE[cur_idx]['name']}")

    except Exception as e:
        print(f"❌ Error: {e}")
        # エラー時は前回のデータを保持（ファイルが存在する場合）
        if not os.path.exists(OUTPUT_PATH):
            # 初回エラー時はデフォルト値を書き込む
            default = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "updated_at_jst": datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M JST"),
                "garmin_time": None,
                "lat": None,
                "lng": None,
                "altitude": None,
                "velocity_kmh": None,
                "current_waypoint_idx": 0,
                "current_waypoint_name": "ルクラ出発",
                "progress_pct": 0,
                "status": "PRE-DEPARTURE",
                "error": str(e),
                "route": ROUTE
            }
            os.makedirs("data", exist_ok=True)
            with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
                json.dump(default, f, ensure_ascii=False, indent=2)
            print(f"Default GPS data written to {OUTPUT_PATH}")
        raise

if __name__ == "__main__":
    fetch_and_convert()
