#!/usr/bin/env python3
"""
Garmin MapShare KMLを取得してdata/gps.jsonに保存するスクリプト
GitHub Actionsから30分ごとに実行される

【標高補正ロジック】
Garmin inReach Mini 2の標高値は気圧高度計のキャリブレーション状態によって
実際の標高と大きく乖離することがある。
そのため、以下のルールで「表示用標高 (display_altitude)」を決定する：

1. 現在地に最も近いウェイポイントを GPS座標（緯度経度）で判定する
2. そのウェイポイントの既知標高と Garmin報告標高の差を計算する
3. 差が CALIBRATION_THRESHOLD (300m) 以内 → Garmin値を信頼（calibrated=True）
4. 差が CALIBRATION_THRESHOLD を超える → ウェイポイントの既知標高を使用（calibrated=False）

キャリブレーション後は自動的に Garmin値に切り替わる。
"""

import requests
import json
import re
import os
import math
from datetime import datetime, timezone
from xml.etree import ElementTree as ET

MAPSHARE_URL = "https://share.garmin.com/feed/share/chikaoeverest"
OUTPUT_PATH = "data/gps.json"

# Garmin標高と既知標高の乖離がこの値（メートル）を超えたら補正値を使用
# 注意: 現在はGarmin実測値をそのまま使用（補正なし）
CALIBRATION_THRESHOLD = 300

# エベレスト南東稜ルートのウェイポイント定義（既知の正確な標高）
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

# カトマンズ（出発前の滞在地）の既知標高
KATHMANDU = {"name": "カトマンズ", "alt": 1400, "lat": 27.7172, "lng": 85.3240}

# 出発前判定の座標範囲（カトマンズ盆地）
KATHMANDU_LAT_RANGE = (27.5, 27.9)
KATHMANDU_LNG_RANGE = (85.0, 85.6)


def haversine_km(lat1, lng1, lat2, lng2):
    """2点間の距離をkm単位で返す（Haversine公式）"""
    R = 6371.0
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lng2 - lng1)
    a = math.sin(dphi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(dlambda / 2) ** 2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1 - a))


def get_data_value(placemark, name):
    """ExtendedDataからname属性に対応するvalueを取得"""
    for data in placemark.findall(".//{http://www.opengis.net/kml/2.2}Data") + placemark.findall(".//Data"):
        if data.get("name") == name:
            val = data.find("{http://www.opengis.net/kml/2.2}value") or data.find("value")
            if val is not None:
                return val.text or ""
    return ""


def get_nearest_waypoint(lat, lng):
    """GPS座標から最も近いウェイポイントのインデックスを返す"""
    min_dist = float("inf")
    nearest_idx = 0
    for i, wp in enumerate(ROUTE):
        d = haversine_km(lat, lng, wp["lat"], wp["lng"])
        if d < min_dist:
            min_dist = d
            nearest_idx = i
    return nearest_idx, min_dist


def get_current_waypoint_by_alt(alt):
    """現在標高から最も近い通過済みウェイポイントのインデックスを返す（フォールバック用）"""
    idx = 0
    for i, wp in enumerate(ROUTE):
        if alt >= wp["alt"] - 200:
            idx = i
        else:
            break
    return idx


def is_in_kathmandu(lat, lng):
    """座標がカトマンズ盆地内かどうかを判定"""
    return (KATHMANDU_LAT_RANGE[0] <= lat <= KATHMANDU_LAT_RANGE[1] and
            KATHMANDU_LNG_RANGE[0] <= lng <= KATHMANDU_LNG_RANGE[1])


def resolve_altitude(garmin_alt, lat, lng):
    """
    表示用標高を決定する。

    Returns:
        display_alt (float): 表示用標高
        calibrated (bool): True=Garmin値を使用, False=補正値を使用
        correction_note (str): 補正の説明
    """
    # カトマンズ滞在中の判定
    if is_in_kathmandu(lat, lng):
        known_alt = KATHMANDU["alt"]
        diff = abs(garmin_alt - known_alt)
        if diff <= CALIBRATION_THRESHOLD:
            return garmin_alt, True, "Garmin値（キャリブレーション済み）"
        else:
            return known_alt, False, f"カトマンズ既知標高を使用（Garmin:{garmin_alt:.0f}m / 既知:{known_alt}m / 差:{diff:.0f}m）"

    # Garmin実測値をそのまま使用（補正なし）
    return garmin_alt, True, "Garmin実測値（補正なし）"


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
        garmin_alt = None
        if elev_str:
            m = re.search(r"([\d.]+)", elev_str)
            if m:
                garmin_alt = float(m.group(1))

        # 速度パース "1.0 km/h"
        velocity = None
        if velocity_str:
            m = re.search(r"([\d.]+)", velocity_str)
            if m:
                velocity = float(m.group(1))

        # ===== 標高補正ロジック =====
        display_alt = garmin_alt
        calibrated = True
        correction_note = "Garmin値（補正なし）"

        if garmin_alt is not None and lat is not None and lng is not None:
            display_alt, calibrated, correction_note = resolve_altitude(garmin_alt, lat, lng)

        print(f"   Garmin Alt: {garmin_alt}m → Display Alt: {display_alt}m")
        print(f"   Calibrated: {calibrated} | {correction_note}")

        # 現在地ウェイポイント判定（Garmin実測標高で判定）
        if display_alt is not None:
            cur_idx = get_current_waypoint_by_alt(display_alt)
        elif lat is not None and lng is not None:
            cur_idx, _ = get_nearest_waypoint(lat, lng)
        else:
            cur_idx = 0

        # 進捗率計算（表示用標高を使用）
        base_alt = 2860
        summit_alt = 8849
        pct = 0
        if display_alt and display_alt > base_alt:
            pct = min(100, round(((display_alt - base_alt) / (summit_alt - base_alt)) * 100))

        # ステータス判定（座標ベース）
        # PRE-DEPARTURE: カトマンズ盆地内（出発前）
        # TREKKING:      ルート上だがベースキャンプ未満
        # ASCENDING:     ベースキャンプ以上（5364m）
        # DEATH ZONE:    7500m以上
        # SUMMIT:        8849m到達
        status = "TREKKING"
        if lat is not None and lng is not None and is_in_kathmandu(lat, lng):
            status = "PRE-DEPARTURE"
        elif display_alt is not None:
            if display_alt >= 8849:
                status = "SUMMIT"
            elif display_alt >= 7500:
                status = "DEATH ZONE"
            elif display_alt >= 5364:
                status = "ASCENDING"

        # 出発からの日数計算（4/4を1日目とする）
        from datetime import date
        DEPARTURE_DATE = date(2026, 4, 4)
        today_jst = datetime.now(timezone.utc).date()
        day_count = (today_jst - DEPARTURE_DATE).days + 1
        day_count = max(1, day_count)

        # CURRENT ALTITUDE表示用ラベル生成
        if status == 'PRE-DEPARTURE':
            location_label = 'カトマンズ　1,400m'
        else:
            alt_val = int(display_alt) if display_alt else ROUTE[cur_idx]['alt']
            location_label = ROUTE[cur_idx]['name'] + '　' + f'{alt_val:,}m'

        # JSON出力
        output = {
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_at_jst": datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M JST"),
            "garmin_time": time_str,
            "lat": lat,
            "lng": lng,
            "altitude": display_alt,          # 表示用標高（補正済み）
            "garmin_altitude": garmin_alt,     # Garmin生データ（参考値）
            "altitude_calibrated": calibrated, # True=Garmin値, False=補正値
            "altitude_note": correction_note,  # 補正の説明
            "velocity_kmh": velocity,
            "current_waypoint_idx": cur_idx,
            "current_waypoint_name": ROUTE[cur_idx]["name"],
            "progress_pct": pct,
            "status": status,
            "day_count": day_count,
            "current_location_label": location_label,
            "route": ROUTE
        }

        os.makedirs("data", exist_ok=True)
        with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
            json.dump(output, f, ensure_ascii=False, indent=2)

        print(f"✅ GPS data saved to {OUTPUT_PATH}")
        print(f"   Alt: {display_alt}m | Status: {status} | Progress: {pct}%")
        print(f"   Waypoint: {ROUTE[cur_idx]['name']}")

    except Exception as e:
        print(f"❌ Error: {e}")
        # エラー時は前回のデータを保持（ファイルが存在する場合）
        if not os.path.exists(OUTPUT_PATH):
            default = {
                "updated_at": datetime.now(timezone.utc).isoformat(),
                "updated_at_jst": datetime.now(timezone.utc).strftime("%Y/%m/%d %H:%M JST"),
                "garmin_time": None,
                "lat": None,
                "lng": None,
                "altitude": None,
                "garmin_altitude": None,
                "altitude_calibrated": False,
                "altitude_note": "データ取得エラー",
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
