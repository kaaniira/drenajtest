# ============================================================
#  BIYOMIMIKRI DRENAJ BACKEND - v14.1 (Python/Flask)
# ============================================================
from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import math
import ee
import os
import google.auth
import statistics
from collections import defaultdict

app = Flask(__name__)
CORS(app)

# --- 1. GEE BAŞLATMA ---
def initialize_gee():
    try:
        # GEE Kimlik Doğrulama
        credentials, project = google.auth.default(
            scopes=['https://www.googleapis.com/auth/earthengine', 'https://www.googleapis.com/auth/cloud-platform']
        )
        ee.Initialize(credentials, project=project)
        print("✅ GEE Başlatıldı - v14.1")
    except Exception as e:
        print(f"❌ GEE Hatası: {e}. 'earthengine authenticate' yapmanız gerekebilir.")

initialize_gee()

# --- 2. EN YAKIN SU KAYNAĞI TESPİTİ VE KOORDİNATLARI ---
def find_nearest_water(lat, lon, max_dist=2000):
    """
    ESA WorldCover verisini kullanarak en yakın su kütlesini (Class 80) bulur
    ve hedef koordinatları döndürür.
    """
    try:
        point = ee.Geometry.Point([lon, lat])
        # ESA WorldCover v200
        wc = ee.ImageCollection("ESA/WorldCover/v200").first()
        water_mask = wc.eq(80) # 80 = Water class (Su sınıfı)
        
        # Su piksellerini vektöre çevir (Bölge sınırlı)
        region = point.buffer(max_dist).bounds()
        water_vectors = water_mask.selfMask().reduceToVectors(
            geometry=region,
            scale=10,
            geometryType='centroid',
            eightConnected=True
        )
        
        if water_vectors.size().getInfo() == 0:
            # Su bulunamazsa varsayılan bir nokta (Merkezin 500m güneyi gibi simüle ediliyor)
            return 500, "Belediye Şehir Hattı", {"lat": lat - 0.004, "lon": lon}
            
        # En yakın mesafeyi ve koordinatı hesapla
        nearest = water_vectors.distance(point, 100).sort('distance').first()
        dist = float(nearest.get('distance').getInfo())
        coords = nearest.geometry().coordinates().getInfo() # [lon, lat]
        
        return round(dist, 1), "Doğal Su Kütlesi (Dere/Deniz)", {"lat": coords[1], "lon": coords[0]}
    except:
        return 350, "Bölgesel Tahliye Hattı", {"lat": lat - 0.003, "lon": lon}

# --- 3. HİDROLİK & İKLİM HESAPLARI ---
def get_rain_series(lat, lon):
    try:
        url = (f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}"
               "&start_date=2015-01-01&end_date=2024-12-31&daily=precipitation_sum&timezone=UTC")
        r = requests.get(url, timeout=10).json()
        return r.get("daily", {}).get("precipitation_sum", [])
    except:
        return [50.0] * 10

# --- 4. ANA ANALİZ ENDPOINT ---
@app.route("/analyze", methods=["POST"])
def analyze():
    try:
        data = request.get_json()
        lat, lon = float(data["lat"]), float(data["lon"])
        radius = float(data.get("radius", 250))
        geom = ee.Geometry.Point([lon, lat]).buffer(radius)
        
        # 1. En Yakın Su Kaynağı ve Koordinatları
        dist_to_water, water_source_name, target_coords = find_nearest_water(lat, lon)
        
        # 2. Arazi Verileri (WorldCover & SRTM)
        wc = ee.ImageCollection("ESA/WorldCover/v200").first()
        dem = ee.Image("USGS/SRTMGL1_003")
        slope = ee.Terrain.slope(dem)
        
        stats = ee.Image.cat([wc.rename("land"), slope.rename("slope")]).reduceRegion(
            reducer=ee.Reducer.mean().combine(ee.Reducer.mode(), sharedInputs=True),
            geometry=geom, scale=10, bestEffort=True
        ).getInfo()
        
        slope_deg = float(stats.get("slope_mean", 2.0))
        slope_pct = math.tan(math.radians(slope_deg)) * 100
        land_cls = int(stats.get("land_mode", 50))
        
        # 3. Yağış & Akış Hesabı
        rain_data = get_rain_series(lat, lon)
        mean_rain = sum(rain_data) / 10 if rain_data else 600
        
        # C Katsayısı (Geçirimsizlik)
        C = 0.85 if land_cls == 50 else 0.40 # Kentsel vs Doğal
        area_ha = (math.pi * radius**2) / 10000
        
        # Rasyonel Metot Q = (C * i * A) / 360
        i_intensity = 65 # mm/h (Gumbel varsayımı)
        Q_flow = (C * i_intensity * area_ha) / 360
        
        # 4. Manning Boru Çapı (Deşarj Mesafesi Dahil)
        # Toplam hat uzunluğu sürtünme direncini artıracağı için çap toleransı eklenir
        total_len = radius + dist_to_water
        n = 0.013
        S_metric = max(0.005, slope_pct / 100)
        D_mm = (((4 ** (5/3)) * n * Q_flow) / (math.pi * math.sqrt(S_metric))) ** (3/8) * 1000
        
        # Uzun hatlarda sürtünme kaybı düzeltmesi (%5-10 güvenlik marjı)
        if total_len > 1000:
            D_mm *= 1.10
        
        # 5. Sistem Seçimi (Biyomimetik Desen)
        selected_system = "dendritic"
        if slope_pct > 12: selected_system = "meandering"
        elif land_cls == 50: selected_system = "reticular"
        elif slope_pct < 2: selected_system = "pinnate"

        # 6. Harita Planı İçin Yol (Path) Koordinatları Oluşturma
        # Merkezden deşarj noktasına bir ana hat yolu
        pipe_path = [
            {"lat": lat, "lon": lon},
            {"lat": target_coords["lat"], "lon": target_coords["lon"]}
        ]

        return jsonify({
            "status": "success",
            "system": selected_system,
            "q_flow": round(Q_flow, 3),
            "diameter_mm": round(D_mm, 0),
            "slope_pct": round(slope_pct, 2),
            "discharge": {
                "distance_m": dist_to_water,
                "target": water_source_name,
                "target_lat": target_coords["lat"],
                "target_lon": target_coords["lon"],
                "total_pipe_m": round(total_len, 1)
            },
            "plan_geometry": {
                "pipe_path": pipe_path,
                "description": f"{selected_system.capitalize()} desenli ana toplayıcı hattı."
            },
            "harvest_m3": round(area_ha * mean_rain * 0.7, 0)
        })

    except Exception as e:
        return jsonify({"status": "error", "msg": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=8080)

application = app
