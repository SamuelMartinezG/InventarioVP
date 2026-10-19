import streamlit as st
import psycopg2
from psycopg2 import IntegrityError
import pandas as pd
from datetime import datetime
import time
import io
import os
import base64
from PIL import Image
from fpdf import FPDF
import math

# ==========================================
# Cargar el ícono cuadrado
try:
    logo_icono = Image.open("icono_app.png")
    st.set_page_config(page_title="Sistema de Insumos - Vet Playas", page_icon=logo_icono, layout="wide")
except FileNotFoundError:
    st.set_page_config(page_title="Sistema de Insumos - Vet Playas", page_icon="🐾", layout="wide")

# ==========================================
DATABASE_URL = st.secrets["DB_URL"]
# ==========================================

def get_connection():
    return psycopg2.connect(DATABASE_URL, connect_timeout=5)

def init_db():
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute('''CREATE TABLE IF NOT EXISTS insumos (
                        id_insumo SERIAL PRIMARY KEY,
                        nombre_articulo TEXT UNIQUE,
                        familia TEXT,
                        departamento TEXT,
                        existencia INTEGER DEFAULT 0)''')
        c.execute('''CREATE TABLE IF NOT EXISTS solicitudes (
                        id_solicitud SERIAL PRIMARY KEY,
                        fecha_hora TEXT,
                        area TEXT,
                        solicitante TEXT,
                        estado TEXT DEFAULT 'PENDIENTE',
                        quien_surte TEXT)''')
        c.execute('''CREATE TABLE IF NOT EXISTS detalle_solicitud (
                        id_detalle SERIAL PRIMARY KEY,
                        id_solicitud INTEGER,
                        id_insumo INTEGER,
                        cantidad_pedida INTEGER,
                        cantidad_entregada INTEGER,
                        FOREIGN KEY(id_solicitud) REFERENCES solicitudes(id_solicitud),
                        FOREIGN KEY(id_insumo) REFERENCES insumos(id_insumo))''')
        c.execute('''CREATE TABLE IF NOT EXISTS personal (
                        id_personal SERIAL PRIMARY KEY,
                        nombre TEXT UNIQUE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS areas (
                        id_area SERIAL PRIMARY KEY,
                        nombre_area TEXT UNIQUE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS historial_entradas (
                        id_entrada SERIAL PRIMARY KEY,
                        fecha_hora TEXT,
                        id_insumo INTEGER,
                        cantidad_agregada INTEGER,
                        tipo_movimiento TEXT,
                        comentarios TEXT,
                        FOREIGN KEY(id_insumo) REFERENCES insumos(id_insumo))''')
        c.execute('''CREATE TABLE IF NOT EXISTS configuracion (
                        parametro TEXT PRIMARY KEY,
                        valor TEXT)''')
        
        c.execute('''CREATE TABLE IF NOT EXISTS proveedores (
                        id_proveedor SERIAL PRIMARY KEY,
                        nombre_proveedor TEXT UNIQUE,
                        dias_entrega FLOAT DEFAULT 3.0)''')
        
        c.execute("SELECT COUNT(*) FROM areas")
        if c.fetchone()[0] == 0:
            for area_inicial in ["HOSPITALIZACIÓN", "CONSULTORIO 1", "CONSULTORIO 2", "LIMPIEZA", "RECEPCIÓN"]:
                c.execute("INSERT INTO areas (nombre_area) VALUES (%s) ON CONFLICT DO NOTHING", (area_inicial,))
                
        c.execute("SELECT COUNT(*) FROM proveedores")
        if c.fetchone()[0] == 0:
            c.execute("INSERT INTO proveedores (nombre_proveedor, dias_entrega) VALUES ('GENERAL', 3.0) ON CONFLICT DO NOTHING")
        
        c.execute("ALTER TABLE solicitudes ADD COLUMN IF NOT EXISTS quien_surte TEXT")
        c.execute("ALTER TABLE detalle_solicitud ADD COLUMN IF NOT EXISTS cantidad_entregada INTEGER")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS ruta_imagen TEXT")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS componentes TEXT")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS imagen_b64 TEXT")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS stock_minimo INTEGER DEFAULT 0")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS stock_maximo INTEGER DEFAULT 0")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS cantidad_estandar INTEGER DEFAULT 0")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS proveedor TEXT")
        # 🚀 NUEVA COLUMNA: Piezas por empaque para compras inteligentes
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS piezas_por_empaque INTEGER DEFAULT 1")
        conn.commit()
    finally:
        conn.close()

init_db()

conn = get_connection()
try:
    df_conf = pd.read_sql_query("SELECT valor FROM configuracion WHERE parametro='gemini_api_key'", conn)
    API_KEY_GLOBAL = df_conf['valor'].iloc[0] if not df_conf.empty else ""
except Exception:
    API_KEY_GLOBAL = ""
finally:
    conn.close()

if not os.path.exists("facturas"):
    os.makedirs("facturas")
if not os.path.exists("img_insumos"):
    os.makedirs("img_insumos")

# ==========================================
# FUNCIONES DE CONEXIÓN CON GEMINI IA
# ==========================================
def obtener_componentes_ia(nombre_producto, api_key):
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key.strip())
        model = genai.GenerativeModel('models/gemini-3.8-flash')
        prompt = f"""Eres un experto farmacólogo veterinario. Tu única tarea es decirme los componentes activos (sustancias) del producto comercial: '{nombre_producto}'.
REGLAS ESTRICTAS:
1. Responde ÚNICAMENTE con los nombres de las sustancias separadas por comas.
2. No uses saludos, ni viñetas, ni explicaciones adicionales.
3. Haz tu mejor esfuerzo por identificarlo.
4. SOLO si estás 100% seguro de que es un accesorio físico (gasas, jeringas) o de plano no existe, responde 'N/A'."""
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "quota" in error_str.lower(): return "ERROR_CUOTA"
        elif "ModuleNotFoundError" in error_str: return "ERROR_LIBRERIA"
        return f"ERROR: {error_str}"

@st.cache_data(ttl=60, show_spinner=False)
def compilar_catalogo_ia(_df_catalogo):
    lista_productos = []
    for _, row in _df_catalogo.iterrows():
        comp = row['componentes'] if pd.notna(row['componentes']) and str(row['componentes']).strip() not in ["", "N/A"] else "Desconocido"
        lista_productos.append(f"{row['nombre_articulo']} [Sustancia: {comp}]")
    return "\n".join(lista_productos)

def recomendar_productos_ia(consulta_medico, df_catalogo, api_key):
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key.strip())
        model = genai.GenerativeModel('models/gemini-3.8-flash')
        catalogo_str = compilar_catalogo_ia(df_catalogo)
        prompt = f"""Eres el asistente médico experto de la clínica Vet Playas. Inventario disponible:
{catalogo_str}

Consulta del médico: "{consulta_medico}"

REGLAS DE CRITERIO CLÍNICO VETERINARIO:
1. Analiza las sustancias activas y nombres comerciales con criterio práctico de consultorio veterinario, NO como un diccionario rígido.
2. Incluye tanto los fármacos de acción primaria directa como aquellos cuya sustancia activa tenga acción secundaria, protectora, regeneradora, lubricante o coadyuvante.
3. Devuelve ÚNICAMENTE los nombres exactos de los productos del inventario anterior separados por el símbolo |. Si ningún producto tiene relación, responde "NINGUNO". Sin explicaciones ni saludos."""
        response = model.generate_content(prompt)
        nombres = response.text.strip().split('|')
        return [n.strip() for n in nombres if n.strip()], None
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "quota" in error_str.lower(): return [], "ERROR_CUOTA"
        elif "ModuleNotFoundError" in error_str: return [], "LIBRERIA_FALTANTE"
        return [], error_str

# ==========================================
# VARIABLES DE SESIÓN
# ==========================================
if 'carrito' not in st.session_state: st.session_state.carrito = []
if 'carrito_entradas' not in st.session_state: st.session_state.carrito_entradas = []
if 'admin_auth' not in st.session_state: st.session_state.admin_auth = False
if 'reset_sol_key' not in st.session_state: st.session_state.reset_sol_key = 0
if 'reset_art_key' not in st.session_state: st.session_state.reset_art_key = 0

try:
    with open("logo.png", "rb") as f:
        st.sidebar.image(f.read(), use_container_width=True)
except FileNotFoundError:
    pass

st.sidebar.title("Navegación")
rol = st.sidebar.selectbox("Selecciona tu perfil:", ["Personal", "Administrador"])

# ==========================================
# VISTAS COMPARTIDAS (PERSONAL Y ADMIN)
# ==========================================
@st.cache_data(ttl=5, show_spinner=False)
def obtener_inventario_rapido():
    conn = get_connection()
    try: 
        return pd.read_sql_query("""
            SELECT id_insumo, nombre_articulo, familia, departamento, 
                   existencia, stock_minimo, stock_maximo, cantidad_estandar, componentes, proveedor, piezas_por_empaque 
            FROM insumos ORDER BY nombre_articulo ASC
        """, conn)
    finally: 
        conn.close()

@st.cache_data(ttl=300, show_spinner=False)
def obtener_foto_individual(id_insumo):
    conn = get_connection()
    try:
        c = conn.cursor()
        c.execute("SELECT imagen_b64 FROM insumos WHERE id_insumo = %s", (int(id_insumo),))
        res = c.fetchone()
        return res[0] if res else None
    finally:
        conn.close()

def mostrar_tarjeta_producto(datos_articulo):
    st.markdown("""
        <style>
        div[data-testid="stVerticalBlockBorderWrapper"] {
            border: 1px solid rgba(128, 128, 128, 0.04) !important;
            border-radius: 10px !important;
        }
        </style>
    """, unsafe_allow_html=True)

    with st.container(border=True):
        col_img, col_info = st.columns([1.2, 2])
        with col_img:
            imagen_b64 = obtener_foto_individual(datos_articulo['id_insumo'])
            if pd.notna(imagen_b64) and str(imagen_b64).strip() != "":
                estilo_img = "max-width: 100%; max-height: 160px; object-fit: contain; margin: 0 auto; display: block;"
                st.markdown(f'<img src="data:image/jpeg;base64,{imagen_b64}" style="{estilo_img}">', unsafe_allow_html=True)
            else:
                st.caption("🚫 Sin foto")
                
        with col_info:
            st.markdown(f"**{datos_articulo['nombre_articulo']}**")
            st.caption(f"🚚 Prov: {datos_articulo.get('proveedor', 'GENERAL')} | 📦 x{int(datos_articulo.get('piezas_por_empaque', 1))}")
            if pd.notna(datos_articulo['componentes']) and str(datos_articulo['componentes']).strip() not in ["", "N/A", "NONE"]:
                st.caption(f"🧬 {datos_articulo['componentes']}")
            
            ex = int(datos_articulo['existencia'])
            s_min = int(datos_articulo.get('stock_minimo', 0))
            
            if ex > s_min:
                st.markdown(f"🟢 **Existencia:** {ex} uds.")
            elif ex > 0 and ex <= s_min:
                st.markdown(f"🟡 **Existencia:** {ex} uds. *(Mínimo)*")
            else:
                st.markdown(f"🔴 **Agotado:** 0 uds.")

def vista_solicitar_insumos():
    st.title("🏥 Registro de Insumos")
    conn = get_connection()
    try:
        insumos_df = pd.read_sql_query("SELECT id_insumo, nombre_articulo, COALESCE(cantidad_estandar, 0) as cantidad_estandar FROM insumos ORDER BY nombre_articulo ASC", conn)
        personal_df = pd.read_sql_query("SELECT nombre FROM personal ORDER BY nombre ASC", conn)
        areas_df = pd.read_sql_query("SELECT nombre_area FROM areas ORDER BY nombre_area ASC", conn)
    finally:
        conn.close()
        
    if insumos_df.empty or personal_df.empty or areas_df.empty:
        st.warning("⚠️ El administrador debe agregar áreas, personal y artículos al catálogo.")
    else:
        st.write("### 1. Datos de la Solicitud")
        col1, col2 = st.columns(2)
        area = col1.selectbox("Área", areas_df['nombre_area'].tolist(), index=None, placeholder="Escribe para buscar el área...", key=f"sel_area_{st.session_state.reset_sol_key}")
        solicitante = col2.selectbox("Nombre del Solicitante", personal_df['nombre'].tolist(), index=None, placeholder="Escribe tu nombre...", key=f"sel_sol_{st.session_state.reset_sol_key}")
        st.divider()
        
        st.write("### 2. Seleccionar Productos")
        col_a, col_b, col_c = st.columns([3, 1.2, 1])
        articulo_seleccionado = col_a.selectbox("Artículo", insumos_df['nombre_articulo'].tolist(), index=None, placeholder="Escribe para buscar y filtrar un artículo...", key=f"sel_art_{st.session_state.reset_art_key}")
        
        if articulo_seleccionado:
            fila_art = insumos_df[insumos_df['nombre_articulo'] == articulo_seleccionado].iloc[0]
            cant_estandar = int(fila_art['cantidad_estandar'])
            id_insumo_sel = int(fila_art['id_insumo'])
            
            if cant_estandar > 0:
                cantidad = col_b.number_input("Cantidad (Fija)", value=cant_estandar, disabled=True, key=f"cant_fija_{id_insumo_sel}_{st.session_state.reset_art_key}")
                col_a.caption(f"🔒 **Entrega estandarizada:** Este artículo se entrega en bloques fijos de **{cant_estandar} uds.**")
            else:
                cantidad = col_b.number_input("Cantidad (A petición)", min_value=1, value=1, step=1, key=f"cant_libre_{id_insumo_sel}_{st.session_state.reset_art_key}")
                col_a.caption("✍️ **Cantidad libre:** Ingresa las unidades necesarias según tus pacientes.")
        else:
            cantidad = col_b.number_input("Cantidad", min_value=1, value=1, disabled=True, key=f"cant_vacia_{st.session_state.reset_art_key}")
            col_a.caption("👆 Escribe en el buscador para filtrar las opciones.")
            
        col_c.write("")
        if col_c.button("➕ Agregar", use_container_width=True):
            if not articulo_seleccionado:
                st.warning("⚠️ Primero busca y selecciona un artículo.")
            else:
                st.session_state.carrito.append({
                    "id_insumo": id_insumo_sel, 
                    "nombre": articulo_seleccionado, 
                    "cantidad": cant_estandar if cant_estandar > 0 else cantidad,
                    "es_fijo": True if cant_estandar > 0 else False
                })
                st.session_state.reset_art_key += 1
                st.rerun()
        
        if len(st.session_state.carrito) > 0:
            st.divider()
            st.write("### 🛒 Resumen de tu Pedido")
            for idx, item in enumerate(st.session_state.carrito):
                c1, c2, c3 = st.columns([4, 2, 1])
                etiqueta_tipo = "🔒 *(Estandarizado)*" if item.get('es_fijo', False) else "✍️ *(A petición)*"
                c1.write(f"{item['nombre']} {etiqueta_tipo}")
                
                if item.get('es_fijo', False):
                    c2.number_input("Cantidad", value=item['cantidad'], disabled=True, key=f"ped_cant_{idx}", label_visibility="collapsed")
                else:
                    nueva_cant = c2.number_input("Cantidad", min_value=1, value=item['cantidad'], step=1, key=f"ped_cant_{idx}", label_visibility="collapsed")
                    if nueva_cant != item['cantidad']: 
                        st.session_state.carrito[idx]['cantidad'] = nueva_cant
                        
                if c3.button("🗑️", key=f"del_ped_{idx}"):
                    st.session_state.carrito.pop(idx)
                    st.rerun()

            st.write("---")
            col_btn1, col_btn2 = st.columns([2, 2])
            if col_btn1.button("✅ Enviar Solicitud Definitiva", type="primary"):
                if not area or not solicitante:
                    st.error("⚠️ Por favor selecciona el Área y el Nombre del Solicitante en la parte superior.")
                else:
                    conn = get_connection()
                    try:
                        c = conn.cursor()
                        fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        c.execute("INSERT INTO solicitudes (fecha_hora, area, solicitante, estado) VALUES (%s, %s, %s, 'PENDIENTE') RETURNING id_solicitud", (fecha_actual, area, solicitante))
                        id_solicitud_nueva = c.fetchone()[0]
                        for item in st.session_state.carrito:
                            c.execute("INSERT INTO detalle_solicitud (id_solicitud, id_insumo, cantidad_pedida) VALUES (%s, %s, %s)", (id_solicitud_nueva, item['id_insumo'], item['cantidad']))
                        conn.commit()
                    finally:
                        conn.close()
                    st.session_state.carrito = []
                    st.session_state.reset_sol_key += 1
                    st.session_state.reset_art_key += 1
                    st.success("¡Solicitud enviada correctamente!")
                    time.sleep(1.5) 
                    st.rerun() 
            
            if col_btn2.button("Vaciar Todo"):
                st.session_state.carrito = []
                st.session_state.reset_sol_key += 1
                st.session_state.reset_art_key += 1
                st.rerun()

def vista_consultar_inventario():
    st.title("📦 Visor de Existencias")
    df_inv = obtener_inventario_rapido()
        
    if not df_inv.empty:
        tab_b1, tab_b2 = st.tabs(["🔍 Búsqueda Normal", "🤖 Asistente Médico (IA)"])
        
        with tab_b1:
            col_f1, col_f2 = st.columns([2, 1])
            busqueda_texto = col_f1.text_input("🔍 Buscar producto o sustancia (Presiona Enter para filtrar):", placeholder="Ej: BRAVECTO, TRAMADOL, GOTAS...")
            deptos_disponibles = ["TODOS"] + sorted(df_inv['departamento'].dropna().unique().tolist())
            depto_filtro = col_f2.selectbox("Filtrar por Departamento:", deptos_disponibles)
            
            df_filtrado = df_inv.copy()
            if depto_filtro != "TODOS":
                df_filtrado = df_filtrado[df_filtrado['departamento'] == depto_filtro]
            
            if busqueda_texto.strip() != "":
                palabras = busqueda_texto.strip().upper().split()
                texto_busqueda = (df_filtrado['nombre_articulo'].fillna('').astype(str) + " " + df_filtrado['componentes'].fillna('').astype(str)).str.upper()
                for palabra in palabras:
                    df_filtrado = df_filtrado[texto_busqueda.str.contains(palabra, na=False, regex=False)]
                    texto_busqueda = texto_busqueda.loc[df_filtrado.index]
            
            total_encontrados = len(df_filtrado)
            st.write("---")
            st.write(f"### 📋 Catálogo Visual ({total_encontrados} productos encontrados)")
            
            if df_filtrado.empty:
                st.warning("No se encontraron productos que coincidan con tu búsqueda.")
            else:
                LIMITE = 40
                if total_encontrados > LIMITE:
                    st.info(f"⚡ Para mantener la máxima velocidad, se muestran solo los primeros **{LIMITE}** resultados.")
                    df_filtrado = df_filtrado.head(LIMITE)

                cols_cat = st.columns(2)
                for idx, (_, row) in enumerate(df_filtrado.iterrows()):
                    with cols_cat[idx % 2]:
                        mostrar_tarjeta_producto(row)
        
        with tab_b2:
            consulta_medico = st.text_input("Consulta a la IA (Ej: 'Antiinflamatorio ocular')")
            if st.button("Consultar ✨", type="primary"):
                if not API_KEY_GLOBAL: 
                    st.error("⚠ IA no configurada.")
                elif not consulta_medico: 
                    st.warning("Escribe tu consulta.")
                else:
                    with st.spinner("Analizando inventario..."):
                        resultados_ia, error_ia = recomendar_productos_ia(consulta_medico, df_inv, API_KEY_GLOBAL)
                        if error_ia: 
                            st.error(f"❌ Error: {error_ia}")
                        elif not resultados_ia or "NINGUNO" in [r.upper() for r in resultados_ia]: 
                            st.warning("No se encontró ningún producto relacionado.")
                        else:
                            st.write("---")
                            cols_ia = st.columns(2)
                            col_idx = 0
                            for nombre_sugerido in resultados_ia:
                                match = df_inv[df_inv['nombre_articulo'].str.upper() == nombre_sugerido.upper()]
                                if not match.empty:
                                    with cols_ia[col_idx % 2]:
                                        mostrar_tarjeta_producto(match.iloc[0])
                                    col_idx += 1
    else: 
        st.info("Catálogo vacío.")

# ==========================================
# MÓDULO: PERSONAL 
# ==========================================
if rol == "Personal":
    menu_personal = st.sidebar.radio("Opciones", ["Solicitar Insumos", "Consultar Inventario"])
    if menu_personal == "Solicitar Insumos": vista_solicitar_insumos()
    elif menu_personal == "Consultar Inventario": vista_consultar_inventario()

# ==========================================
# MÓDULO: ADMINISTRADOR
# ==========================================
elif rol == "Administrador":
    if not st.session_state.admin_auth:
        st.title("🔒 Acceso Restringido")
        with st.form("form_login"):
            pwd_ingresada = st.text_input("Contraseña", type="password")
            if st.form_submit_button("Ingresar"):
                if pwd_ingresada == "vetplayas": 
                    st.session_state.admin_auth = True
                    st.rerun()
                else: st.error("❌ Contraseña incorrecta.")
    else:
        st.sidebar.markdown("---")
        if st.sidebar.button("🔴 Cerrar Sesión"):
            st.session_state.admin_auth = False
            st.rerun()
            
        st.title("⚙ Panel de Administración")
        menu_admin = st.sidebar.radio("Opciones", ["Surtir Solicitudes", "Solicitar Insumos", "Consultar Inventario", "Entradas / Compras", "Catálogo de Insumos", "Ventas y Reportes", "Personal, Áreas y Proveedores", "Configuración IA"])
        
        if menu_admin == "Surtir Solicitudes":
            st.subheader("📦 Solicitudes Pendientes")
            conn = get_connection()
            try:
                lista_personal = [""] + pd.read_sql_query("SELECT nombre FROM personal ORDER BY nombre ASC", conn)['nombre'].tolist()
                solicitudes_df = pd.read_sql_query("SELECT * FROM solicitudes WHERE UPPER(estado) = 'PENDIENTE' ORDER BY fecha_hora ASC", conn)
                if solicitudes_df.empty: st.info("No hay solicitudes.")
                else:
                    for _, row in solicitudes_df.iterrows():
                        id_sol = row['id_solicitud']
                        with st.expander(f"🔴 Solicitud #{id_sol} | Área: {row['area']} | Solicitante: {row['solicitante']} ({row['fecha_hora']})"):
                            detalles_df = pd.read_sql_query(f"""
                                SELECT d.id_detalle, d.cantidad_pedida, i.nombre_articulo, i.id_insumo, COALESCE(i.cantidad_estandar, 0) as cantidad_estandar 
                                FROM detalle_solicitud d JOIN insumos i ON d.id_insumo = i.id_insumo WHERE d.id_solicitud = {id_sol}
                            """, conn)
                            with st.form(key=f"form_surtir_{id_sol}"):
                                quien_surte = st.selectbox("¿Quién entrega?", lista_personal, key=f"quien_{id_sol}")
                                st.markdown("**Detalle de artículos solicitados:**")
                                cantidades_a_entregar = {}
                                for _, det_row in detalles_df.iterrows():
                                    c1_i, c2_i, c3_i = st.columns([3.5, 1, 1.5])
                                    est_val = int(det_row['cantidad_estandar'])
                                    etiqueta_est = f"🔒 *(Estandarizado: {est_val} uds)*" if est_val > 0 else "✍️ *(A petición)*"
                                    c1_i.markdown(f"**{det_row['nombre_articulo']}** {etiqueta_est}")
                                    c2_i.write(f"Pedido: **{det_row['cantidad_pedida']}**")
                                    cantidades_a_entregar[det_row['id_detalle']] = c3_i.number_input("Entregar", min_value=0, value=int(det_row['cantidad_pedida']), step=1, key=f"ent_{id_sol}_{det_row['id_detalle']}", label_visibility="collapsed")
                                if st.form_submit_button(f"✅ Surtir #{id_sol}"):
                                    if not quien_surte: st.error("⚠️ Selecciona quién entrega.")
                                    else:
                                        c = conn.cursor()
                                        c.execute("UPDATE solicitudes SET estado = 'SURTIDA', quien_surte = %s WHERE id_solicitud = %s", (quien_surte, id_sol))
                                        for _, d_row in detalles_df.iterrows():
                                            cant = cantidades_a_entregar[d_row['id_detalle']]
                                            c.execute("UPDATE detalle_solicitud SET cantidad_entregada = %s WHERE id_detalle = %s", (cant, d_row['id_detalle']))
                                            c.execute("UPDATE insumos SET existencia = existencia - %s WHERE id_insumo = %s", (cant, d_row['id_insumo']))
                                        conn.commit()
                                        st.success("Surtido.")
                                        time.sleep(1)
                                        st.rerun()
            finally: conn.close()

        elif menu_admin == "Solicitar Insumos": vista_solicitar_insumos()
        elif menu_admin == "Consultar Inventario": vista_consultar_inventario()

        elif menu_admin == "Entradas / Compras":
            st.subheader("📥 Registrar Ingreso y Enseñar a la IA")
            ent_key = st.session_state.get("reset_ent_key", 0)
            
            conn = get_connection()
            try:
                insumos_df = pd.read_sql_query("SELECT id_insumo, nombre_articulo, existencia, stock_minimo, stock_maximo, componentes, cantidad_estandar, familia, departamento, imagen_b64, COALESCE(proveedor, 'GENERAL') as proveedor, COALESCE(piezas_por_empaque, 1) as piezas_por_empaque FROM insumos ORDER BY nombre_articulo ASC", conn)
                proveedores_df = pd.read_sql_query("SELECT nombre_proveedor FROM proveedores ORDER BY nombre_proveedor ASC", conn)
                lista_provs = proveedores_df['nombre_proveedor'].tolist() if not proveedores_df.empty else ["GENERAL"]
                
                with st.container(border=True):
                    opciones_art = ["➕ REGISTRAR PRODUCTO NUEVO..."] + (insumos_df['nombre_articulo'].tolist() if not insumos_df.empty else [])
                    try:
                        seleccion_art = st.selectbox("🔍 Artículo (escribe para buscar o presiona Enter para nuevo):", opciones_art, index=None, placeholder="Escribe el nombre o presentación...", accept_new_options=True, key=f"ent_sel_art_{ent_key}")
                    except TypeError:
                        seleccion_art = st.selectbox("🔍 Artículo (escribe para buscar o selecciona '➕ REGISTRAR PRODUCTO NUEVO...'):", opciones_art, index=None, placeholder="Escribe el nombre o presentación...", key=f"ent_sel_art_{ent_key}")
                    
                    es_existente = False
                    es_nuevo = False
                    if seleccion_art:
                        if not insumos_df.empty and seleccion_art in insumos_df['nombre_articulo'].values: es_existente = True
                        else: es_nuevo = True
                    
                    if es_existente:
                        datos_prod = insumos_df[insumos_df['nombre_articulo'] == seleccion_art].iloc[0]
                        id_ins = int(datos_prod['id_insumo'])
                        st.markdown("---")
                        col_img_p, col_inf_p = st.columns([1, 4])
                        with col_img_p:
                            img_b64 = datos_prod.get('imagen_b64', None)
                            if pd.notna(img_b64) and str(img_b64).strip() != "":
                                st.markdown(f'<img src="data:image/jpeg;base64,{img_b64}" style="max-width: 100%; max-height: 120px; object-fit: contain;">', unsafe_allow_html=True)
                            else: st.caption("🚫 Sin imagen")
                        with col_inf_p:
                            st.markdown(f"✅ **{datos_prod['nombre_articulo']}** | 🚚 Prov: {datos_prod['proveedor']}")
                            st.caption(f"📁 {datos_prod['familia']} - {datos_prod['departamento']} | 🔒 Estandarizado: {int(datos_prod['cantidad_estandar'])} | 📦 {int(datos_prod['piezas_por_empaque'])} pzas x caja")
                            st.markdown(f"📦 **Existencia actual:** `{int(datos_prod['existencia'])} uds` *(Mín: {int(datos_prod['stock_minimo'])} | Máx: {int(datos_prod['stock_maximo'])})*")
                        
                        st.markdown("---")
                        col1, col2 = st.columns(2)
                        cajas = col1.number_input("Cajas/Empaques a ingresar", min_value=1, value=1, key=f"cajas_ex_{ent_key}")
                        pzas = col2.number_input("Piezas x Empaque", min_value=1, value=int(datos_prod['piezas_por_empaque']), key=f"pzas_ex_{ent_key}")
                        
                        if st.button("➕ Agregar al Ingreso", type="primary", key=f"btn_add_ex_{ent_key}"):
                            st.session_state.carrito_entradas.append({"id_insumo": id_ins, "nombre": datos_prod['nombre_articulo'], "cajas": cajas, "piezas_caja": pzas, "total": cajas * pzas})
                            st.session_state["reset_ent_key"] = ent_key + 1
                            st.rerun()

                    elif es_nuevo:
                        st.info("🆕 **Producto nuevo detectado.** Se guardará en el catálogo y en la factura:")
                        nombre_inicial = "" if seleccion_art == "➕ REGISTRAR PRODUCTO NUEVO..." else seleccion_art.strip().upper()
                        
                        col_n1, col_n2, col_n3, col_n4 = st.columns(4)
                        n_nuevo = col_n1.text_input("Nombre / Presentación", value=nombre_inicial, key=f"nom_nv_{ent_key}")
                        prov_nuevo = col_n2.selectbox("Proveedor", lista_provs, key=f"prov_nv_{ent_key}")
                        f_nueva = col_n3.selectbox("Familia", ["PRODUCTOS", "SERVICIOS"], key=f"fam_nv_{ent_key}")
                        d_nuevo = col_n4.selectbox("Depto", ["EXAMENES", "SERVICIOS", "MEDICAMENTO", "ALIMENTOS", "VACUNAS", "ROPA", "ACCESORIOS"], index=2, key=f"dep_nv_{ent_key}")
                        
                        comp_key_state = f"comp_ia_ent_{ent_key}"
                        if comp_key_state not in st.session_state: st.session_state[comp_key_state] = ""
                            
                        c_ia1, c_ia2 = st.columns([3, 1])
                        comp_nuevo = c_ia1.text_input("🧬 Componentes Activos", value=st.session_state[comp_key_state], key=f"in_comp_nv_{ent_key}")
                        c_ia2.write("")
                        if c_ia2.button("✨ Sugerir con IA", key=f"btn_ia_nv_{ent_key}"):
                            if not n_nuevo.strip(): st.warning("Escribe primero el nombre del producto.")
                            else:
                                res_ia = obtener_componentes_ia(n_nuevo, API_KEY_GLOBAL)
                                if "ERROR" not in res_ia: st.session_state[comp_key_state] = res_ia.upper(); st.rerun()
                                else: st.error(res_ia)
                        
                        col_m1, col_m2, col_m3 = st.columns(3)
                        s_min = col_m1.number_input("Stock Mínimo (Alerta)", min_value=0, value=2, key=f"min_nv_{ent_key}")
                        s_max = col_m2.number_input("Stock Máximo (Meta)", min_value=1, value=10, key=f"max_nv_{ent_key}")
                        s_est = col_m3.number_input("Entrega Estandarizada (0 = Libre)", min_value=0, value=0, key=f"est_nv_{ent_key}")
                        
                        img_nueva = st.file_uploader("📸 Subir Imagen del Producto", type=["jpg", "png", "jpeg"], key=f"img_nv_{ent_key}")
                        
                        st.markdown("---")
                        col_c1, col_c2 = st.columns(2)
                        c_n = col_c1.number_input("Cajas a ingresar", min_value=1, value=1, key=f"cajas_nv_{ent_key}")
                        p_n = col_c2.number_input("Piezas x Caja (Se guarda en catálogo)", min_value=1, value=1, key=f"pzas_nv_{ent_key}")
                        
                        if st.button("➕ Crear Catálogo y Agregar", type="primary", key=f"btn_add_nv_{ent_key}"):
                            if not n_nuevo.strip(): st.error("⚠️ El nombre es obligatorio.")
                            else:
                                try:
                                    c = conn.cursor()
                                    comp_clean = comp_nuevo.strip().upper() if comp_nuevo else ""
                                    img_b64_nv = base64.b64encode(img_nueva.getvalue()).decode() if img_nueva else None
                                    c.execute("""INSERT INTO insumos (nombre_articulo, familia, departamento, componentes, existencia, stock_minimo, stock_maximo, cantidad_estandar, imagen_b64, proveedor, piezas_por_empaque) 
                                        VALUES (%s, %s, %s, %s, 0, %s, %s, %s, %s, %s, %s) RETURNING id_insumo""", (n_nuevo.strip().upper(), f_nueva, d_nuevo, comp_clean, s_min, s_max, s_est, img_b64_nv, prov_nuevo, p_n))
                                    id_creado = c.fetchone()[0]
                                    conn.commit()
                                    st.session_state.carrito_entradas.append({"id_insumo": id_creado, "nombre": n_nuevo.strip().upper(), "cajas": c_n, "piezas_caja": p_n, "total": c_n * p_n})
                                    st.session_state["reset_ent_key"] = ent_key + 1
                                    st.success("✅ Creado y agregado.")
                                    time.sleep(1); st.rerun()
                                except IntegrityError: st.error("⚠️ Ya existe.")

                if st.session_state.carrito_entradas:
                    st.divider()
                    st.write("### 🛒 Artículos en esta Factura")
                    for idx, item in enumerate(st.session_state.carrito_entradas):
                        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])
                        c1.write(item['nombre'])
                        n_c = c2.number_input("Cajas", min_value=1, value=item['cajas'], key=f"ec_{idx}", label_visibility="collapsed")
                        n_p = c3.number_input("Pzas", min_value=1, value=item['piezas_caja'], key=f"ep_{idx}", label_visibility="collapsed")
                        if n_c != item['cajas'] or n_p != item['piezas_caja']:
                            st.session_state.carrito_entradas[idx].update({"cajas": n_c, "piezas_caja": n_p, "total": n_c * n_p})
                            st.rerun()
                        c4.write(f"**{item['total']} uds**")
                        if c5.button("🗑️", key=f"de_{idx}"): st.session_state.carrito_entradas.pop(idx); st.rerun()

                    tipo = st.selectbox("Tipo de Movimiento", ["Compra / Factura", "Ajuste"])
                    if tipo == "Compra / Factura":
                        dias_tardo = st.number_input("🤖 IA: ¿Cuántos días tardó el proveedor en entregar esto? (Promedio Móvil)", min_value=1, value=3, help="El sistema usará esto para aprender y avisarte antes de que te quedes sin stock.")
                    coment = st.text_input("Folio de Factura / Comentarios")
                    arch = st.file_uploader("Adjuntar evidencia (PDF/Foto)", type=["pdf", "png", "jpg"])
                    if st.button("✅ Guardar e Ingresar Stock", type="primary"):
                        c = conn.cursor()
                        fh = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        info_a = ""
                        if arch:
                            nom = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{arch.name}"
                            with open(os.path.join("facturas", nom), "wb") as f: f.write(arch.getbuffer())
                            info_a = f" | Arch: {nom}"
                        for item in st.session_state.carrito_entradas:
                            c.execute("INSERT INTO historial_entradas (fecha_hora, id_insumo, cantidad_agregada, tipo_movimiento, comentarios) VALUES (%s, %s, %s, %s, %s)", (fh, item['id_insumo'], item['total'], tipo, f"{item['cajas']} cj de {item['piezas_caja']}. Ref: {coment}{info_a}"))
                            c.execute("UPDATE insumos SET existencia = existencia + %s WHERE id_insumo = %s", (item['total'], item['id_insumo']))
                            if tipo == "Compra / Factura":
                                c.execute("""UPDATE proveedores SET dias_entrega = (dias_entrega + %s) / 2.0 
                                             WHERE nombre_proveedor = (SELECT proveedor FROM insumos WHERE id_insumo = %s)""", (float(dias_tardo), item['id_insumo']))
                        conn.commit()
                        st.session_state.carrito_entradas = []
                        st.session_state["reset_ent_key"] = ent_key + 1
                        st.success("✅ Stock actualizado e IA entrenada.")
                        time.sleep(1); st.rerun()
            finally: conn.close()

        elif menu_admin == "Catálogo de Insumos":
            st.subheader("Catálogo de Insumos")
            tab_cat, tab_masiva = st.tabs(["📋 Gestión de Catálogo", "🚀 Carga Masiva (Excel)"])
            
            conn = get_connection()
            try:
                proveedores_df = pd.read_sql_query("SELECT nombre_proveedor FROM proveedores ORDER BY nombre_proveedor ASC", conn)
                lista_provs = proveedores_df['nombre_proveedor'].tolist() if not proveedores_df.empty else ["GENERAL"]
            finally: conn.close()
            
            with tab_cat:
                with st.expander("➕ Agregar Nuevo Artículo al Catálogo", expanded=False):
                    c1, c2, c3, c_p = st.columns(4)
                    n_nuevo = c1.text_input("Nombre del Producto", key="nn_art")
                    p_nuevo = c_p.selectbox("Proveedor", lista_provs, key="np_art")
                    f_nuevo = c2.selectbox("Familia", ["PRODUCTOS", "SERVICIOS"], key="nf_art")
                    d_nuevo = c3.selectbox("Depto", ["EXAMENES", "SERVICIOS", "MEDICAMENTO", "ALIMENTOS", "VACUNAS", "ROPA", "ACCESORIOS"], key="nd_art")
                    comp_nuevo = st.text_input("Componentes", key="n_comp")
                    
                    c4, c5, c6, c7 = st.columns(4)
                    s_min_nuevo = c4.number_input("Stock Mínimo (Alerta)", min_value=0, value=2, key="n_min_art")
                    s_max_nuevo = c5.number_input("Stock Máximo (Meta)", min_value=1, value=10, key="n_max_art")
                    s_est_nuevo = c6.number_input("Entrega (0 = Libre)", min_value=0, value=0, key="n_est_art")
                    pzas_emp_nuevo = c7.number_input("Pzas x Empaque", min_value=1, value=1, help="Ej: Si compras frascos de 60 pastillas, pon 60", key="n_pemp_art")
                    
                    if st.button("Guardar Nuevo Producto", type="primary", key="btn_guardar_nuevo"):
                        if n_nuevo:
                            conn = get_connection()
                            try:
                                c = conn.cursor()
                                comp_nuevo_clean = comp_nuevo.strip().upper() if comp_nuevo else ""
                                c.execute("""INSERT INTO insumos (nombre_articulo, familia, departamento, componentes, existencia, stock_minimo, stock_maximo, cantidad_estandar, proveedor, piezas_por_empaque) 
                                             VALUES (%s, %s, %s, %s, 0, %s, %s, %s, %s, %s)""", 
                                          (n_nuevo.strip().upper(), f_nuevo, d_nuevo, comp_nuevo_clean, s_min_nuevo, s_max_nuevo, s_est_nuevo, p_nuevo, pzas_emp_nuevo))
                                conn.commit()
                                st.success("Agregado exitosamente.")
                                time.sleep(1); st.rerun()
                            except IntegrityError: st.error("Ya existe.")
                            finally: conn.close()
                        else: st.error("El nombre es obligatorio.")

                st.write("### 📋 Directorio de Productos")
                conn = get_connection()
                try: df_cat = pd.read_sql_query("SELECT * FROM insumos ORDER BY nombre_articulo", conn)
                finally: conn.close()
                
                if not df_cat.empty:
                    df_display = df_cat[['nombre_articulo', 'proveedor', 'stock_minimo', 'stock_maximo']].copy()
                    df_display['Empaque'] = df_cat['piezas_por_empaque'].fillna(1).apply(lambda x: f"x{int(x)}")
                    df_display['Imagen'] = df_cat['imagen_b64'].apply(lambda x: "✅" if pd.notna(x) and str(x).strip() != "" else "🚫")
                    df_display.columns = ['Producto', 'Proveedor', 'Mínimo', 'Máximo', 'Pzas Empaque', 'Imagen']
                    st.dataframe(df_display, use_container_width=True, hide_index=True)
                    
                    st.write("---")
                    st.write("### ✏️ Editar / Eliminar Artículo")
                    
                    df_cat['tiene_img'] = df_cat['imagen_b64'].apply(lambda x: 1 if pd.notna(x) and str(x).strip() != "" else 0)
                    df_cat_ordenado = df_cat.sort_values(by=['tiene_img', 'nombre_articulo'])
                    art_edit = st.selectbox("Selecciona un artículo para modificarlo (Los que faltan por foto aparecen primero):", df_cat_ordenado['nombre_articulo'].tolist(), key="sel_art_edit")
                    
                    if art_edit:
                        datos_art = df_cat_ordenado[df_cat_ordenado['nombre_articulo'] == art_edit].iloc[0]
                        if st.session_state.get("last_edit_art") != art_edit:
                            st.session_state["edit_comp_val"] = datos_art['componentes'] if pd.notna(datos_art['componentes']) else ""
                            st.session_state["last_edit_art"] = art_edit
                            st.session_state["file_uploader_key"] = int(time.time())
                        
                        col_e1, col_e2, col_e3, col_e_p = st.columns(4)
                        n_edit = col_e1.text_input("Nombre", value=datos_art['nombre_articulo'], key="ed_nom_art")
                        prov_actual = datos_art.get('proveedor', 'GENERAL')
                        prov_index = lista_provs.index(prov_actual) if prov_actual in lista_provs else 0
                        p_edit = col_e_p.selectbox("Proveedor", lista_provs, index=prov_index, key="ed_prov_art")
                        fam_index = ["PRODUCTOS", "SERVICIOS"].index(datos_art['familia']) if datos_art['familia'] in ["PRODUCTOS", "SERVICIOS"] else 0
                        f_edit = col_e2.selectbox("Familia", ["PRODUCTOS", "SERVICIOS"], index=fam_index, key="ed_fam_art")
                        dept_opts = ["EXAMENES", "SERVICIOS", "MEDICAMENTO", "ALIMENTOS", "VACUNAS", "ROPA", "ACCESORIOS"]
                        dept_index = dept_opts.index(datos_art['departamento']) if datos_art['departamento'] in dept_opts else 2
                        d_edit = col_e3.selectbox("Depto", dept_opts, index=dept_index, key="ed_dep_art")
                        
                        c_ia1, c_ia2 = st.columns([3, 1])
                        c_edit = c_ia1.text_input("Componentes", value=st.session_state["edit_comp_val"], key="ed_comp_art")
                        c_ia2.write("")
                        if c_ia2.button("✨ Sugerir con IA", key="btn_ia_sug"):
                            res = obtener_componentes_ia(n_edit, API_KEY_GLOBAL)
                            if "ERROR" not in res: st.session_state["edit_comp_val"] = res.upper(); st.rerun()
                            else: st.error(res)
                        
                        col_e4, col_e5, col_e6, col_e7 = st.columns(4)
                        min_edit = col_e4.number_input("Stock Mín", min_value=0, value=int(datos_art['stock_minimo']), key="ed_min_art")
                        max_edit = col_e5.number_input("Stock Máx", min_value=1, value=max(1, int(datos_art['stock_maximo'])), key="ed_max_art")
                        est_actual = int(datos_art['cantidad_estandar']) if pd.notna(datos_art.get('cantidad_estandar')) else 0
                        est_edit = col_e6.number_input("Entrega (0=Libre)", min_value=0, value=est_actual, key="ed_est_art")
                        emp_actual = int(datos_art.get('piezas_por_empaque', 1)) if pd.notna(datos_art.get('piezas_por_empaque')) else 1
                        emp_edit = col_e7.number_input("Pzas x Empaque", min_value=1, value=emp_actual, key="ed_emp_art")
                        
                        uploader_key = st.session_state.get("file_uploader_key", 0)
                        img_upload = st.file_uploader("Subir / Actualizar Imagen", type=["jpg", "png", "jpeg"], key=f"img_up_{uploader_key}")
                        
                        col_btn_e1, col_btn_e2 = st.columns(2)
                        if col_btn_e1.button("✅ Guardar Cambios al Artículo", type="primary", key="btn_guardar_ed"):
                            if n_edit.strip() == "": st.error("El nombre no puede estar vacío.")
                            else:
                                conn = get_connection()
                                try:
                                    c = conn.cursor()
                                    c_edit_clean = c_edit.strip().upper() if c_edit else "" 
                                    if img_upload:
                                        img_b64 = base64.b64encode(img_upload.getvalue()).decode()
                                        c.execute("""UPDATE insumos SET nombre_articulo=%s, familia=%s, departamento=%s, componentes=%s, 
                                                     stock_minimo=%s, stock_maximo=%s, cantidad_estandar=%s, proveedor=%s, piezas_por_empaque=%s, imagen_b64=%s WHERE id_insumo=%s""", 
                                                  (n_edit.strip().upper(), f_edit, d_edit, c_edit_clean, min_edit, max_edit, est_edit, p_edit, emp_edit, img_b64, int(datos_art['id_insumo'])))
                                    else:
                                        c.execute("""UPDATE insumos SET nombre_articulo=%s, familia=%s, departamento=%s, componentes=%s, 
                                                     stock_minimo=%s, stock_maximo=%s, cantidad_estandar=%s, proveedor=%s, piezas_por_empaque=%s WHERE id_insumo=%s""", 
                                                  (n_edit.strip().upper(), f_edit, d_edit, c_edit_clean, min_edit, max_edit, est_edit, p_edit, emp_edit, int(datos_art['id_insumo'])))
                                    conn.commit()
                                    st.success("Artículo actualizado.")
                                    st.session_state["last_edit_art"] = None
                                    time.sleep(1); st.rerun()
                                except IntegrityError: st.error("Ese nombre ya está en uso.")
                                finally: conn.close()
                                
                        if col_btn_e2.button("🗑️ Eliminar Artículo", key="btn_elim_ed"):
                            conn = get_connection()
                            try:
                                c = conn.cursor(); c.execute("DELETE FROM insumos WHERE id_insumo = %s", (int(datos_art['id_insumo']),)); conn.commit()
                                st.success("Artículo eliminado."); st.session_state["last_edit_art"] = None; time.sleep(1); st.rerun()
                            except IntegrityError: st.error("⚠ No se puede eliminar: tiene historial.")
                            finally: conn.close()
                else: st.info("No hay artículos en el catálogo.")
                    
            with tab_masiva:
                st.write("**Carga Masiva desde Excel**")
                st.info("El archivo Excel debe tener la columna: PRODUCTO. Opcionales: PROVEEDOR, MINIMO, MAXIMO, PZAS_EMPAQUE, ESTANDAR, FAMILIA, DEPARTAMENTO.")
                archivo_masivo = st.file_uploader("Sube tu catálogo completo", type=["xls", "xlsx"])
                if archivo_masivo:
                    df_masivo = pd.read_excel(archivo_masivo)
                    df_masivo.columns = [str(c).strip().upper() for c in df_masivo.columns]
                    if 'PRODUCTO' in df_masivo.columns:
                        if st.button("🚀 Iniciar Carga de Catálogo", type="primary"):
                            conn = get_connection()
                            try:
                                c = conn.cursor()
                                count_ok = 0; count_errores = 0
                                for _, row in df_masivo.iterrows():
                                    p = str(row['PRODUCTO']).strip().upper()
                                    if pd.isna(p) or p == "NAN" or p == "": continue
                                    f = str(row.get('FAMILIA', 'PRODUCTOS')).strip().upper() if 'FAMILIA' in df_masivo.columns and pd.notna(row.get('FAMILIA')) else 'PRODUCTOS'
                                    d = str(row.get('DEPARTAMENTO', 'MEDICAMENTO')).strip().upper() if 'DEPARTAMENTO' in df_masivo.columns and pd.notna(row.get('DEPARTAMENTO')) else 'MEDICAMENTO'
                                    pr = str(row.get('PROVEEDOR', 'GENERAL')).strip().upper() if 'PROVEEDOR' in df_masivo.columns and pd.notna(row.get('PROVEEDOR')) else 'GENERAL'
                                    
                                    try: s_min = int(float(row.get('MINIMO', 0))) if 'MINIMO' in df_masivo.columns else 0
                                    except: s_min = 0
                                    try: s_max = int(float(row.get('MAXIMO', 0))) if 'MAXIMO' in df_masivo.columns else 0
                                    except: s_max = 0
                                    try: s_est = int(float(row.get('ESTANDAR', 0))) if 'ESTANDAR' in df_masivo.columns else 0
                                    except: s_est = 0
                                    try: pzas_e = int(float(row.get('PZAS_EMPAQUE', 1))) if 'PZAS_EMPAQUE' in df_masivo.columns else 1
                                    except: pzas_e = 1
                                    
                                    try:
                                        c.execute("INSERT INTO proveedores (nombre_proveedor) VALUES (%s) ON CONFLICT DO NOTHING", (pr,))
                                        c.execute("""INSERT INTO insumos (nombre_articulo, familia, departamento, existencia, stock_minimo, stock_maximo, cantidad_estandar, proveedor, piezas_por_empaque) 
                                            VALUES (%s, %s, %s, 0, %s, %s, %s, %s, %s) ON CONFLICT (nombre_articulo) 
                                            DO UPDATE SET stock_minimo = EXCLUDED.stock_minimo, stock_maximo = EXCLUDED.stock_maximo, cantidad_estandar = EXCLUDED.cantidad_estandar, proveedor = EXCLUDED.proveedor, piezas_por_empaque = EXCLUDED.piezas_por_empaque
                                        """, (p, f, d, s_min, s_max, s_est, pr, pzas_e))
                                        count_ok += 1
                                    except Exception: count_errores += 1
                                conn.commit()
                                st.success(f"✅ ¡Éxito! {count_ok} artículos subidos/actualizados.")
                            finally: conn.close()
                    else: st.error("❌ El archivo no tiene la columna 'PRODUCTO'.")

        elif menu_admin == "Ventas y Reportes":
            st.subheader("📊 Ventas, Reportes y Compras IA")
            tab_ia, tab_rep, tab_ven = st.tabs(["🤖 Compras Inteligentes (PDF)", "📊 Reportes Clásicos", "📉 Descuento Excel Ventas"])
            
            with tab_ia:
                st.info("💡 **MRP Inteligente:** El sistema calcula el promedio de consumo de los últimos 30 días, cruza los datos con el tiempo de entrega de cada proveedor, y calcula matemáticamente cuántos **empaques/cajas** debes comprar hoy.")
                
                conn = get_connection()
                try:
                    df_consumo = pd.read_sql_query("""
                        SELECT d.id_insumo, SUM(d.cantidad_entregada) as total_30d 
                        FROM detalle_solicitud d 
                        JOIN solicitudes s ON d.id_solicitud = s.id_solicitud 
                        WHERE s.estado = 'SURTIDA' AND s.fecha_hora::timestamp >= NOW() - INTERVAL '30 days'
                        GROUP BY d.id_insumo
                    """, conn)
                    df_inv = pd.read_sql_query("""
                        SELECT i.id_insumo, i.nombre_articulo, i.existencia, i.stock_minimo, i.stock_maximo, 
                               COALESCE(i.proveedor, 'GENERAL') as proveedor, 
                               COALESCE(p.dias_entrega, 3.0) as dias_entrega,
                               COALESCE(i.piezas_por_empaque, 1) as piezas_empaque
                        FROM insumos i LEFT JOIN proveedores p ON i.proveedor = p.nombre_proveedor
                    """, conn)
                finally: conn.close()
                
                df_m = pd.merge(df_inv, df_consumo, on='id_insumo', how='left').fillna({'total_30d': 0})
                df_m['consumo_diario'] = df_m['total_30d'] / 30.0
                df_m['min_ia'] = (df_m['consumo_diario'] * df_m['dias_entrega']) + (df_m['consumo_diario'] * 2)
                df_m['min_real'] = df_m.apply(lambda x: x['min_ia'] if x['total_30d'] > 0 else x['stock_minimo'], axis=1)
                
                df_pedir = df_m[df_m['existencia'] <= df_m['min_real']].copy()
                
                if df_pedir.empty:
                    st.success("🎉 ¡Tu bodega está perfecta! Según la Inteligencia Artificial y tus ventas, tienes abasto suficiente de todo.")
                else:
                    df_pedir['max_ia'] = df_pedir['min_real'] + (df_pedir['consumo_diario'] * 15)
                    df_pedir['max_real'] = df_pedir.apply(lambda x: x['max_ia'] if x['total_30d'] > 0 else x['stock_maximo'], axis=1)
                    
                    # 🚀 MAGIA LOGÍSTICA: Dividimos los faltantes entre las piezas por empaque y redondeamos hacia arriba.
                    df_pedir['Unidades_Faltantes'] = df_pedir['max_real'] - df_pedir['existencia']
                    df_pedir['Empaques_A_Pedir'] = df_pedir.apply(lambda x: max(1, math.ceil(x['Unidades_Faltantes'] / x['piezas_empaque'])), axis=1)
                    
                    df_pedir['Artículo'] = df_pedir['nombre_articulo']
                    df_pedir['Proveedor'] = df_pedir['proveedor']
                    
                    # 🚀 Ocultamos la existencia en la tabla y en el PDF
                    df_final_pdf = df_pedir[['Proveedor', 'Artículo', 'Empaques_A_Pedir']].sort_values(by=['Proveedor', 'Artículo'])
                    df_final_pdf.rename(columns={'Empaques_A_Pedir': 'Cant. (Cajas/Frascos)'}, inplace=True)
                    st.dataframe(df_final_pdf, hide_index=True)
                    
                    def crear_pdf_compras(df_pedidos):
                        pdf = FPDF()
                        pdf.set_auto_page_break(auto=True, margin=15)
                        for prov in df_pedidos['Proveedor'].unique():
                            pdf.add_page()
                            pdf.set_font("Arial", "B", 14)
                            p_text = str(prov).encode('latin-1', 'replace').decode('latin-1')
                            pdf.cell(0, 10, f"ORDEN DE COMPRA | PROVEEDOR: {p_text}", ln=True, align="L")
                            pdf.set_font("Arial", "", 10)
                            pdf.cell(0, 10, f"FECHA DE SOLICITUD: {datetime.now().strftime('%d/%m/%Y')}", ln=True, align="L")
                            pdf.ln(5)
                            
                            pdf.set_font("Arial", "B", 10)
                            pdf.cell(140, 10, "Producto", border=1)
                            pdf.cell(40, 10, "Cant. (Empaques)", border=1, align="C")
                            pdf.ln()
                            
                            pdf.set_font("Arial", "", 9)
                            df_prov = df_pedidos[df_pedidos['Proveedor'] == prov]
                            for _, row in df_prov.iterrows():
                                nombre = str(row['Artículo']).encode('latin-1', 'replace').decode('latin-1')[:65]
                                pdf.cell(140, 10, nombre, border=1)
                                pdf.cell(40, 10, str(int(row['Cant. (Cajas/Frascos)'])), border=1, align="C")
                                pdf.ln()
                        return bytes(pdf.output(dest='S'), 'latin-1')

                    pdf_bytes = crear_pdf_compras(df_final_pdf)
                    st.download_button(label="📄 Descargar PDF para WhatsApp", data=pdf_bytes, file_name=f"Compras_Inteligentes_{datetime.now().strftime('%Y%m%d')}.pdf", mime="application/pdf", type="primary")

            with tab_rep:
                conn = get_connection()
                try:
                    df_compras = pd.read_sql_query("""SELECT nombre_articulo as "Artículo", familia as "Familia", departamento as "Departamento", existencia as "Stock_Actual", stock_minimo as "Mínimo", stock_maximo as "Máximo", (stock_maximo - existencia) as "Cantidad_A_Pedir" FROM insumos WHERE existencia <= stock_minimo ORDER BY departamento ASC, nombre_articulo ASC""", conn)
                    df_inv = pd.read_sql_query("SELECT nombre_articulo, existencia, stock_minimo, stock_maximo, COALESCE(cantidad_estandar, 0) as cantidad_estandar FROM insumos ORDER BY nombre_articulo ASC", conn)
                    df_sol = pd.read_sql_query("SELECT s.id_solicitud as Folio, s.fecha_hora as Fecha, s.area as Área, s.solicitante as Solicitante, s.estado as Estado, i.nombre_articulo as Artículo, d.cantidad_entregada as Entregado FROM solicitudes s JOIN detalle_solicitud d ON s.id_solicitud = d.id_solicitud JOIN insumos i ON d.id_insumo = i.id_insumo", conn)
                    df_ent = pd.read_sql_query("SELECT h.fecha_hora as Fecha, i.nombre_articulo as Artículo, h.cantidad_agregada as Cantidad, h.tipo_movimiento as Movimiento FROM historial_entradas h JOIN insumos i ON h.id_insumo = i.id_insumo", conn)
                finally: conn.close()
                def conv_df(df):
                    out = io.BytesIO()
                    df.to_excel(out, index=False)
                    return out.getvalue()
                c1, c2, c3 = st.columns(3)
                if not df_inv.empty: c1.download_button("📥 Inventario Total", data=conv_df(df_inv), file_name="Inventario.xlsx")
                if not df_sol.empty: c2.download_button("📥 Salidas (Consumo)", data=conv_df(df_sol), file_name="Salidas.xlsx")
                if not df_ent.empty: c3.download_button("📥 Entradas", data=conv_df(df_ent), file_name="Entradas.xlsx")
                st.write("---")
                st.write("**Lista Clásica de Compras (Lógica Manual):**")
                if not df_compras.empty: st.download_button("🛒 Bajar Excel Manual", data=conv_df(df_compras), file_name="Pedido_Manual.xlsx")
                else: st.success("Inventario ok.")

            with tab_ven:
                arch = st.file_uploader("Sube tu reporte de ventas", type=["xls", "xlsx"], key="up_ventas_excel")
                if arch:
                    df_v = pd.read_excel(arch)
                    df_v.columns = [str(c).strip().upper() for c in df_v.columns]
                    if 'PRODUCTO' in df_v.columns and 'VOLUMEN' in df_v.columns:
                        if st.button("Aplicar Descuentos", type="primary", key="btn_desc_ventas"):
                            conn = get_connection()
                            try:
                                c = conn.cursor()
                                ok = 0
                                for _, row in df_v.iterrows():
                                    p = str(row['PRODUCTO']).strip().upper()
                                    try: v = int(float(row['VOLUMEN']))
                                    except: v = 0
                                    if pd.notna(p) and v > 0 and p != "NAN":
                                        c.execute("SELECT id_insumo FROM insumos WHERE nombre_articulo = %s", (p,))
                                        res = c.fetchone()
                                        if res:
                                            c.execute("UPDATE insumos SET existencia = existencia - %s WHERE id_insumo = %s", (v, res[0]))
                                            ok += 1
                                conn.commit()
                                st.success(f"✅ {ok} artículos descontados del inventario.")
                            finally: conn.close()
                    else: st.error("❌ Faltan las columnas PRODUCTO y VOLUMEN en el archivo.")

        elif menu_admin == "Personal, Áreas y Proveedores":
            st.subheader("👥 Gestión Operativa")
            tab_pers, tab_areas, tab_prov = st.tabs(["👥 Personal", "🏥 Áreas", "🚚 Proveedores"])
            
            with tab_pers:
                with st.form("form_agregar_personal", clear_on_submit=True):
                    n_pers = st.text_input("Nuevo Colaborador")
                    if st.form_submit_button("Guardar") and n_pers:
                        conn = get_connection()
                        try:
                            c = conn.cursor(); c.execute("INSERT INTO personal (nombre) VALUES (%s)", (n_pers.strip().upper(),)); conn.commit(); st.success("Registrado."); time.sleep(1); st.rerun()
                        except IntegrityError: st.error("Ya existe.")
                        finally: conn.close()
                st.divider()
                conn = get_connection()
                try: df_personal = pd.read_sql_query("SELECT id_personal, nombre FROM personal ORDER BY nombre ASC", conn)
                finally: conn.close()
                if not df_personal.empty:
                    st.dataframe(df_personal, use_container_width=True, hide_index=True)
                    st.write("---")
                    persona_sel = st.selectbox("Editar Personal:", df_personal['nombre'].tolist(), key="sel_pers_mod")
                    if persona_sel:
                        id_p = int(df_personal[df_personal['nombre'] == persona_sel].iloc[0]['id_personal'])
                        col_ed1, col_ed2 = st.columns([3, 1])
                        n_nom = col_ed1.text_input("Modificar nombre:", value=persona_sel, key="in_pers_mod")
                        col_btn1, col_btn2 = st.columns(2)
                        if col_btn1.button("Actualizar", type="primary", key="btn_act_pers"):
                            conn = get_connection()
                            try: c = conn.cursor(); c.execute("UPDATE personal SET nombre = %s WHERE id_personal = %s", (n_nom.strip().upper(), id_p)); conn.commit(); st.rerun()
                            except IntegrityError: st.error("Ese nombre ya existe.")
                            finally: conn.close()
                        if col_btn2.button("🗑️ Eliminar", key="btn_del_pers"):
                            conn = get_connection()
                            try: c = conn.cursor(); c.execute("DELETE FROM personal WHERE id_personal = %s", (id_p,)); conn.commit(); st.rerun()
                            finally: conn.close()

            with tab_areas:
                with st.form("form_agregar_area", clear_on_submit=True):
                    n_area = st.text_input("Nueva Área")
                    if st.form_submit_button("Guardar") and n_area:
                        conn = get_connection()
                        try: c = conn.cursor(); c.execute("INSERT INTO areas (nombre_area) VALUES (%s)", (n_area.strip().upper(),)); conn.commit(); st.rerun()
                        except IntegrityError: st.error("Ya existe.")
                        finally: conn.close()
                st.divider()
                conn = get_connection()
                try: df_areas = pd.read_sql_query("SELECT id_area, nombre_area FROM areas ORDER BY nombre_area ASC", conn)
                finally: conn.close()
                if not df_areas.empty:
                    st.dataframe(df_areas, use_container_width=True, hide_index=True)
                    area_sel = st.selectbox("Editar Área:", df_areas['nombre_area'].tolist(), key="sel_area_mod")
                    if area_sel:
                        id_a = int(df_areas[df_areas['nombre_area'] == area_sel].iloc[0]['id_area'])
                        n_nom_a = st.text_input("Modificar área:", value=area_sel, key="in_area_mod")
                        col_ba1, col_ba2 = st.columns(2)
                        if col_ba1.button("Actualizar", type="primary", key="btn_act_area"):
                            conn = get_connection()
                            try: c = conn.cursor(); c.execute("UPDATE areas SET nombre_area = %s WHERE id_area = %s", (n_nom_a.strip().upper(), id_a)); conn.commit(); st.rerun()
                            except IntegrityError: st.error("Ya existe.")
                            finally: conn.close()
                        if col_ba2.button("🗑️ Eliminar", key="btn_del_area"):
                            conn = get_connection()
                            try: c = conn.cursor(); c.execute("DELETE FROM areas WHERE id_area = %s", (id_a,)); conn.commit(); st.rerun()
                            finally: conn.close()

            with tab_prov:
                with st.form("form_agregar_prov", clear_on_submit=True):
                    n_prov = st.text_input("Nuevo Proveedor")
                    if st.form_submit_button("Guardar") and n_prov:
                        conn = get_connection()
                        try: c = conn.cursor(); c.execute("INSERT INTO proveedores (nombre_proveedor) VALUES (%s)", (n_prov.strip().upper(),)); conn.commit(); st.rerun()
                        except IntegrityError: st.error("Ya existe.")
                        finally: conn.close()
                st.divider()
                conn = get_connection()
                try: df_prov = pd.read_sql_query("SELECT id_proveedor, nombre_proveedor, dias_entrega FROM proveedores ORDER BY nombre_proveedor ASC", conn)
                finally: conn.close()
                if not df_prov.empty:
                    st.dataframe(df_prov, use_container_width=True, hide_index=True)
                    prov_sel = st.selectbox("Editar Proveedor:", df_prov['nombre_proveedor'].tolist(), key="sel_prov_mod")
                    if prov_sel:
                        id_pr = int(df_prov[df_prov['nombre_proveedor'] == prov_sel].iloc[0]['id_proveedor'])
                        n_nom_pr = st.text_input("Modificar Proveedor:", value=prov_sel, key="in_prov_mod")
                        col_bp1, col_bp2 = st.columns(2)
                        if col_bp1.button("Actualizar", type="primary", key="btn_act_prov"):
                            conn = get_connection()
                            try: c = conn.cursor(); c.execute("UPDATE proveedores SET nombre_proveedor = %s WHERE id_proveedor = %s", (n_nom_pr.strip().upper(), id_pr)); conn.commit(); st.rerun()
                            except IntegrityError: st.error("Ya existe.")
                            finally: conn.close()
                        if col_bp2.button("🗑️ Eliminar", key="btn_del_prov"):
                            conn = get_connection()
                            try: c = conn.cursor(); c.execute("DELETE FROM proveedores WHERE id_proveedor = %s", (id_pr,)); conn.commit(); st.rerun()
                            finally: conn.close()
            
        elif menu_admin == "Configuración IA":
            n_llave = st.text_input("API Key:", value=API_KEY_GLOBAL, type="password")
            if st.button("Guardar"):
                conn = get_connection()
                try:
                    c = conn.cursor(); c.execute("INSERT INTO configuracion (parametro, valor) VALUES (%s, %s) ON CONFLICT (parametro) DO UPDATE SET valor = EXCLUDED.valor", ('gemini_api_key', n_llave.strip()))
                    conn.commit(); st.success("Guardado."); time.sleep(1); st.rerun()
                finally: conn.close()
