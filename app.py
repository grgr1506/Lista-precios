import pandas as pd
from flask import Flask, jsonify, request, render_template
from flask_cors import CORS
import os
import re
import json

app = Flask(__name__, template_folder='templates')
CORS(app)

# --- ARCHIVOS ---
FILE_PRECIOS = "data_precios.xlsx"    # Costos Odoo (Ya en USD)
FILE_REGLAS = "data_reglas.xlsx"      # EXCEL MAESTRO DE REGLAS
FILE_DB_MANUAL = "db_manual.json"     # Ediciones rápidas web

# --- DEFAULTS ---
MARGEN_DEFECTO = 0.20
FLETE_NORMAL = 0.08      
FLETE_PELIGROSO = 0.11   
COSTO_ENVASE_STD_1KG = 0.15  
COSTO_ENVASE_STD_5KG = 0.40  

# --- CACHÉ GLOBAL (Para búsqueda ultra rápida) ---
CACHE_PRODUCTOS = []

# --- GESTIÓN DE DATOS MANUALES ---
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

# --- LÓGICA DE NEGOCIO ---
def detectar_info(nombre):
    nombre = str(nombre).upper()
    match = re.search(r'(\d+)\s*KG', nombre)
    if match: kg = float(match.group(1))
    elif '1LT' in nombre or '1 LT' in nombre: kg = 1.0
    elif 'GALON' in nombre: kg = 3.785
    elif '250ML' in nombre: kg = 0.25
    else: kg = 1.0 
    
    tipo = 'LIQUIDO' if any(x in nombre for x in ['LIQ', 'ACIDO', 'JARABE', 'ESENCIA', 'SOLUCION']) else 'POLVO'
    peligroso = any(x in nombre for x in ['SULFURICO', 'NITRICO', 'CLORHIDRICO', 'AMONIACO'])
    return kg, tipo, peligroso

def cargar_reglas_excel():
    if not os.path.exists(FILE_REGLAS): return {}
    try:
        df = pd.read_excel(FILE_REGLAS)
        df.columns = [str(c).strip().lower() for c in df.columns]
        
        col_prod = next((c for c in df.columns if 'prod' in c or 'nombre' in c), None)
        col_margen = next((c for c in df.columns if 'margen' in c), None)
        col_envase = next((c for c in df.columns if 'envase' in c or 'extra' in c), None)
        col_flete = next((c for c in df.columns if 'flete' in c or 'cobrar' in c), None)
        col_manual = next((c for c in df.columns if 'manual' in c or 'costo' in c), None)
        
        reglas = {}
        if not col_prod: return {}

        for _, row in df.iterrows():
            nombre = str(row[col_prod]).upper().strip()
            
            m = MARGEN_DEFECTO
            if col_margen:
                val = pd.to_numeric(row[col_margen], errors='coerce')
                if not pd.isna(val): m = val / 100 if val > 1 else val
            
            e = 0.0
            if col_envase:
                val = pd.to_numeric(row[col_envase], errors='coerce')
                if not pd.isna(val): e = val
                
            f = True
            if col_flete:
                val = str(row[col_flete]).upper().strip()
                if 'NO' in val or 'FALSE' in val: f = False
                
            cm = 0.0
            if col_manual:
                val = pd.to_numeric(row[col_manual], errors='coerce')
                if not pd.isna(val): cm = val

            reglas[nombre] = {
                "margen": m,
                "envase_extra": e,
                "cobrar_flete": f,
                "costo_manual": cm
            }
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
        if not col_nombre: 
            col_nombre = next((c for c in df.columns if 'producto' in c and 'categor' not in c and 'cod' not in c), None)

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
            
            kg, _, _ = detectar_info(nombre_full)
            nombre_upper = nombre_full.upper()
            
            categoria = str(row[col_cat]).strip().upper() if col_cat and pd.notna(row[col_cat]) else 'GENERAL'
            marca = str(row[col_marca]).strip().upper() if col_marca and pd.notna(row[col_marca]) else 'GENERICO'
            codigo = str(row[col_codigo]).strip() if col_codigo and pd.notna(row[col_codigo]) else 'S/C'
            unidad = str(row[col_unidad]).strip().upper() if col_unidad and pd.notna(row[col_unidad]) else 'KG'

            costo_base_usd = 0.0
            
            if nombre_upper in reglas_excel and reglas_excel[nombre_upper]['costo_manual'] > 0:
                costo_base_usd = reglas_excel[nombre_upper]['costo_manual']
                costo_base_usd = costo_base_usd * 1.05 
            else:
                costo_base_usd = pd.to_numeric(row[col_costo], errors='coerce') or 0.0

            temp_data.append({
                'nombre': nombre_full,
                'categoria': categoria,
                'marca': marca,
                'codigo': codigo,
                'unidad_tipo': unidad,
                'costo_usd': costo_base_usd,
                'kg': kg
            })

            nombre_base = re.sub(r'\s*X?\s*\d+\.?\d*\s*(KG|G|L|LT|GALON|ML)\s*$', '', nombre_upper).strip()
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

            regla = reglas_excel.get(nombre_u, {"margen": MARGEN_DEFECTO, "envase_extra": 0.0, "cobrar_flete": True})
            if nombre in db_manual and 'margen' in db_manual[nombre]: regla['margen'] = db_manual[nombre]['margen']

            costo_envase_unit = 0.0
            if regla['envase_extra'] > 0: costo_envase_unit = regla['envase_extra'] / kg
            else:
                if kg == 1: costo_envase_unit = COSTO_ENVASE_STD_1KG / 1
                elif kg == 5: costo_envase_unit = COSTO_ENVASE_STD_5KG / 5

            costo_op = costo + costo_envase_unit
            precio_lima = costo_op * (1 + regla['margen'])
            
            _, _, peligroso = detectar_info(nombre)
            flete = 0.0
            if regla['cobrar_flete']: flete = FLETE_PELIGROSO if peligroso else FLETE_NORMAL
            
            precio_prov = precio_lima + flete

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
                "flete_status": "SI" if regla['cobrar_flete'] else "NO"
            })

        unicos = {}
        for r in resultados:
            clave = f"{r['nombre']}_{r['presentacion']}"
            if clave not in unicos or r['precio_lima'] > unicos[clave]['precio_lima']: unicos[clave] = r
                
        lista_final = list(unicos.values())
        lista_final.sort(key=lambda x: x['nombre'])
        return lista_final

    except Exception as e:
        print(f"Error procesando data: {e}")
        return []

def actualizar_cache():
    """Función que recarga la RAM solo cuando es necesario"""
    global CACHE_PRODUCTOS
    CACHE_PRODUCTOS = procesar_excel()
    print(f"🔄 Caché actualizado. Productos cargados en RAM: {len(CACHE_PRODUCTOS)}")

# Cargar caché al iniciar el servidor
actualizar_cache()

# --- RUTAS ---
@app.route('/')
def home(): return render_template('index.html')

@app.route('/buscar')
def buscar():
    # Búsqueda instantánea desde la memoria RAM (CACHE)
    q = request.args.get('q', '').upper().strip()
    
    if not q: 
        return jsonify(CACHE_PRODUCTOS)
        
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
    actualizar_cache() # Recargar RAM
    return jsonify({"mensaje": "✅ Costos Odoo actualizados"})

@app.route('/subir-reglas', methods=['POST'])
def subir_reglas():
    f = request.files['archivo']
    ext = os.path.splitext(f.filename)[1]
    global FILE_REGLAS
    FILE_REGLAS = f"data_reglas{ext}"
    f.save(FILE_REGLAS)
    actualizar_cache() # Recargar RAM
    return jsonify({"mensaje": "✅ Reglas Maestras actualizadas"})

@app.route('/api/editar-margen', methods=['POST'])
def editar_margen():
    d = request.json
    guardar_db_manual(d['nombre'], 'margen', float(d['margen'])/100)
    actualizar_cache() # Recargar RAM
    return jsonify({"success": True})

if __name__ == '__main__':
    app.run(debug=True, port=5000)