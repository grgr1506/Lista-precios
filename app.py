import pandas as pd
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import os
import re
import json
import unicodedata

app = Flask(__name__, template_folder='templates')
CORS(app)

# --- ARCHIVOS ---
FILE_PRECIOS = "data_precios.xlsx"    
FILE_REGLAS = "data_reglas.xlsx"      
FILE_DB_MANUAL = "db_manual.json"     

# =========================================================
# 📘 DICCIONARIOS DE TARIFAS (Tu Panel de Control Interno)
# =========================================================
MARGEN_DEFECTO = 0.20

# 1. Diccionario de Envases (USD)
TARIFAS_ENVASE = {
    "GALONERA": 0.50,
    "BOLSA": 0.15,
    "FRASCO": 0.30,
    "NINGUNO": 0.0
}
COSTO_ENVASE_STD_1KG = 0.15  
COSTO_ENVASE_STD_5KG = 0.40  

# 2. Diccionario de Fletes (USD por Kg)
TARIFAS_FLETE = {
    "F1": 0.08,   # Flete Estándar
    "F2": 0.15,   # Flete Especial/Largo
    "F0": 0.00    # Flete Gratis (Puesto en provincia)
}

# 3. Penalidad por Material Peligroso
RECARGO_PELIGROSO = 0.03  
# =========================================================

CACHE_PRODUCTOS = []

def cargar_db_manual():
    if os.path.exists(FILE_DB_MANUAL):
        try:
            with open(FILE_DB_MANUAL, 'r', encoding='utf-8') as f: return json.load(f)
        except: return {}
    return {}

def guardar_db_manual(nombre, campo, valor):
    datos = cargar_db_manual()
    if nombre not in datos: datos[nombre] = {}
    datos[nombre][campo] = valor
    with open(FILE_DB_MANUAL, 'w', encoding='utf-8') as f:
        json.dump(datos, f, ensure_ascii=False, indent=4)

def detectar_info_basica(nombre, codigo=""):
    nombre = str(nombre).upper()
    codigo = str(codigo).upper().strip()
    
    match = re.search(r'X?\s*(\d+\.?\d*)\s*(KG|G|L|LT|ML)', nombre)
    if match: 
        kg = float(match.group(1))
        if match.group(2) in ['G', 'ML']: kg = kg / 1000.0
    else:
        match_cod = re.search(r'-(\d{3})$', codigo)
        if match_cod: kg = float(match_cod.group(1))
        else:
            if '1LT' in nombre or '1 LT' in nombre: kg = 1.0
            elif 'GALON' in nombre: kg = 3.785
            elif '250ML' in nombre: kg = 0.25
            else: kg = 1.0 
    return kg

def normalizar_texto(texto):
    """Limpia tildes, espacios extra y pasa a mayúsculas para evitar errores de tipeo de columnas"""
    if pd.isna(texto): return ""
    t = str(texto).strip().upper()
    return ''.join(c for c in unicodedata.normalize('NFD', t) if unicodedata.category(c) != 'Mn')

def cargar_reglas_excel():
    if not os.path.exists(FILE_REGLAS): return {}
    try:
        if FILE_REGLAS.endswith('.csv'): df = pd.read_csv(FILE_REGLAS)
        else: df = pd.read_excel(FILE_REGLAS)
            
        # Normalizamos los nombres de las columnas para buscar coincidencias exactas y seguras
        df.columns = [normalizar_texto(c) for c in df.columns]
        
        # BÚSQUEDA ESTRICTA DE PARÁMETROS: Solo lee si las columnas se llaman exactamente así
        col_prod = "PRODUCTO" if "PRODUCTO" in df.columns else None
        col_margen = "MARGEN" if "MARGEN" in df.columns else None
        col_envase = "TIPO ENVASE" if "TIPO ENVASE" in df.columns else None
        
        # Flete (Soporta si le pusieron el punto o no en "Cód. Flete")
        col_flete = next((c for c in df.columns if c in ["COD. FLETE", "COD FLETE"]), None) 
        
        col_peligroso = "PELIGROSO" if "PELIGROSO" in df.columns else None
        col_manual = "COSTO FABRICACION" if "COSTO FABRICACION" in df.columns else None
        
        reglas = {}
        if not col_prod: 
            print("⚠️ Error: No se encontró la columna 'Producto' en el Excel Maestro.")
            return {}

        for _, row in df.iterrows():
            if pd.isna(row[col_prod]): continue
            nombre = str(row[col_prod]).upper().strip()
            
            # Margen
            m = MARGEN_DEFECTO
            if col_margen and not pd.isna(row[col_margen]):
                val = pd.to_numeric(row[col_margen], errors='coerce')
                if not pd.isna(val): m = val / 100 if val > 1 else val
            
            # Envase
            e = ""
            if col_envase and not pd.isna(row[col_envase]):
                e = str(row[col_envase]).upper().strip()
                
            # Cod Flete
            f = "F1" 
            if col_flete and not pd.isna(row[col_flete]):
                f = str(row[col_flete]).upper().strip()
                
            # Peligroso
            p = False
            if col_peligroso and not pd.isna(row[col_peligroso]):
                val_p = str(row[col_peligroso]).upper().strip()
                if val_p in ['SI', 'YES', 'TRUE', '1']: p = True
                
            # Costo Manual
            cm = 0.0
            if col_manual and not pd.isna(row[col_manual]):
                val = pd.to_numeric(row[col_manual], errors='coerce')
                if not pd.isna(val): cm = val

            dict_regla = {
                "margen": m,
                "envase": e,
                "cod_flete": f,
                "peligroso": p,
                "costo_manual": cm
            }
            
            reglas[nombre] = dict_regla
            nombre_base = re.sub(r'\s*X?\s*\d+\.?\d*\s*(KG|G|L|LT|GALON|ML)\s*$', '', nombre).strip()
            if nombre_base not in reglas: reglas[nombre_base] = dict_regla
                
        return reglas
    except Exception as e:
        print(f"Error reglas: {e}")
        return {}

def cargar_df_seguro(filepath):
    try:
        if filepath.endswith('.csv'): df_temp = pd.read_csv(filepath, header=None)
        else: df_temp = pd.read_excel(filepath, header=None)
            
        header_row_idx = 0
        for idx, row in df_temp.iterrows():
            row_str = ' '.join(str(x).lower() for x in row.values if pd.notna(x))
            if ('producto' in row_str or 'name' in row_str) and ('c/u' in row_str or 'cost' in row_str or 'precio' in row_str):
                header_row_idx = idx
                break
                
        if filepath.endswith('.csv'): return pd.read_csv(filepath, header=header_row_idx)
        else: return pd.read_excel(filepath, header=header_row_idx)
    except Exception as e:
        print(f"Error cargando archivo seguro: {e}")
        return None

def procesar_excel():
    if not os.path.exists(FILE_PRECIOS): return []
    try:
        db_manual = cargar_db_manual()
        reglas_excel = cargar_reglas_excel()
        
        df = cargar_df_seguro(FILE_PRECIOS)
        if df is None: return []
            
        df.columns = [str(c).strip().lower() for c in df.columns]
        
        col_nombre = next((c for c in df.columns if c in ['producto', 'nombre', 'name']), None)
        if not col_nombre: col_nombre = next((c for c in df.columns if 'producto' in c and 'categor' not in c and 'cod' not in c), None)
        col_costo = next((c for c in df.columns if 'c/u' in c or 'usd' in c or '$' in c or 'cost' in c or 'unit' in c), None)
        col_cat = next((c for c in df.columns if 'categor' in c), None)
        col_marca = next((c for c in df.columns if 'marca' in c), None)
        col_codigo = next((c for c in df.columns if 'codigo' in c or 'código' in c), None)
        col_unidad = next((c for c in df.columns if 'unidad' in c), None)
        
        if not col_costo or not col_nombre: return []

        precios_maestros = {}
        temp_data = []

        for _, row in df.iterrows():
            nombre_full = str(row[col_nombre]).strip()
            if nombre_full == 'nan' or not nombre_full: continue
            
            categoria = str(row[col_cat]).strip().upper() if col_cat and pd.notna(row[col_cat]) else 'GENERAL'
            marca = str(row[col_marca]).strip().upper() if col_marca and pd.notna(row[col_marca]) else 'GENERICO'
            codigo = str(row[col_codigo]).strip() if col_codigo and pd.notna(row[col_codigo]) else 'S/C'
            unidad = str(row[col_unidad]).strip().upper() if col_unidad and pd.notna(row[col_unidad]) else 'KG'

            kg = detectar_info_basica(nombre_full, codigo)
            
            nombre_upper = nombre_full.upper()
            nombre_base = re.sub(r'\s*X?\s*\d+\.?\d*\s*(KG|G|L|LT|GALON|ML)\s*$', '', nombre_upper).strip()

            regla_maestra = reglas_excel.get(nombre_upper, reglas_excel.get(nombre_base))
            
            costo_base_usd = 0.0
            if regla_maestra and regla_maestra['costo_manual'] > 0:
                costo_base_usd = regla_maestra['costo_manual'] * 1.05 
            else:
                costo_base_usd = pd.to_numeric(row[col_costo], errors='coerce') or 0.0

            temp_data.append({
                'nombre': nombre_full, 'categoria': categoria, 'marca': marca, 'codigo': codigo,
                'unidad_tipo': unidad, 'costo_usd': costo_base_usd, 'kg': kg
            })

            if costo_base_usd > 0.0001: precios_maestros[nombre_base] = costo_base_usd

        resultados = []
        for item in temp_data:
            nombre = item['nombre']
            nombre_u = nombre.upper()
            kg = item['kg']
            costo = item['costo_usd']
            nombre_base = re.sub(r'\s*X?\s*\d+\.?\d*\s*(KG|G|L|LT|GALON|ML)\s*$', '', nombre_u).strip()

            if costo <= 0.0001:
                if nombre_base in precios_maestros: costo = precios_maestros[nombre_base]
                else: continue

            regla_encontrada = reglas_excel.get(nombre_u, reglas_excel.get(nombre_base, {
                "margen": MARGEN_DEFECTO, "envase": "", "cod_flete": "F1", "peligroso": False
            }))
            
            regla = dict(regla_encontrada) 

            if nombre in db_manual and 'margen' in db_manual[nombre]: 
                regla['margen'] = db_manual[nombre]['margen']

            # 1. Costo Envase
            costo_envase_unit = 0.0
            etiqueta_envase = regla['envase']
            if etiqueta_envase in TARIFAS_ENVASE:
                costo_envase_unit = TARIFAS_ENVASE[etiqueta_envase] / kg
            else:
                if kg == 1: costo_envase_unit = COSTO_ENVASE_STD_1KG / 1
                elif kg == 5: costo_envase_unit = COSTO_ENVASE_STD_5KG / 5

            # 2. Costo Operativo y Lima
            costo_op = costo + costo_envase_unit
            precio_lima = costo_op * (1 + regla['margen'])
            
            # 3. Flete (Código + Peligroso)
            codigo_flete = regla['cod_flete']
            flete_base = TARIFAS_FLETE.get(codigo_flete, 0.08) 
            
            if regla['peligroso']: 
                flete_base += RECARGO_PELIGROSO
                
            precio_prov = precio_lima + flete_base

            resultados.append({
                "nombre": nombre,
                "categoria": item['categoria'],
                "marca": item['marca'],
                "codigo": item['codigo'],
                "unidad_tipo": item['unidad_tipo'],
                "margen": f"{round(regla['margen']*100, 1)}",
                "precio_lima": round(precio_lima, 2),
                "precio_aqp": round(precio_prov, 2),
                "precio_tru": round(precio_prov, 2),
                "presentacion": kg,
                "flete_status": "NO" if codigo_flete == "F0" else "SI"
            })

        unicos = {}
        for r in resultados:
            clave = f"{r['codigo']}_{r['nombre']}_{r['presentacion']}"
            if clave not in unicos or r['precio_lima'] > unicos[clave]['precio_lima']: 
                unicos[clave] = r
                
        lista_final = list(unicos.values())
        
        lista_final.sort(key=lambda x: (
            re.sub(r'\s*X?\s*\d+\.?\d*\s*(KG|G|L|LT|GALON|ML)\s*$', '', x['nombre'].upper()).strip(),
            -x['presentacion']
        ))
        
        return lista_final

    except Exception as e:
        print(f"Error procesando data: {e}")
        return []

def actualizar_cache():
    global CACHE_PRODUCTOS
    CACHE_PRODUCTOS = procesar_excel()
    print(f"🔄 Caché actualizado. Productos cargados en RAM: {len(CACHE_PRODUCTOS)}")

actualizar_cache()

@app.route('/')
def home(): return render_template('index.html')

@app.route('/buscar')
def buscar():
    q = request.args.get('q', '').upper().strip()
    if not q: return jsonify(CACHE_PRODUCTOS)
        
    palabras = q.split()
    res = [p for p in CACHE_PRODUCTOS if all(pal in p['nombre'].upper() for pal in palabras)]
    return jsonify(res)

@app.route('/subir-precios', methods=['POST'])
def subir_precios():
    f = request.files['archivo']
    ext = os.path.splitext(f.filename)[1]
    global FILE_PRECIOS
    FILE_PRECIOS = f"data_precios{ext}"
    f.save(FILE_PRECIOS)
    actualizar_cache() 
    return jsonify({"mensaje": "✅ Costos Odoo actualizados"})

@app.route('/subir-reglas', methods=['POST'])
def subir_reglas():
    f = request.files['archivo']
    ext = os.path.splitext(f.filename)[1]
    global FILE_REGLAS
    FILE_REGLAS = f"data_reglas{ext}"
    f.save(FILE_REGLAS)
    actualizar_cache() 
    return jsonify({"mensaje": "✅ Reglas Maestras actualizadas"})

@app.route('/api/editar-margen', methods=['POST'])
def editar_margen():
    d = request.json
    guardar_db_manual(d['nombre'], 'margen', float(d['margen'])/100)
    actualizar_cache() 
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(debug=True, port=5000)