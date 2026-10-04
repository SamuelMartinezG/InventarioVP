import streamlit as st
import psycopg2
from psycopg2 import IntegrityError
import pandas as pd
from datetime import datetime
import time
import io
import os
import base64

st.set_page_config(page_title="Sistema de Insumos - Vet Playas", page_icon="logo.png", layout="wide")

# ==========================================
# 🔴 PEGA TU ENLACE DE SUPABASE AQUÍ ADENTRO:
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
        
        c.execute("ALTER TABLE solicitudes ADD COLUMN IF NOT EXISTS quien_surte TEXT")
        c.execute("ALTER TABLE detalle_solicitud ADD COLUMN IF NOT EXISTS cantidad_entregada INTEGER")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS ruta_imagen TEXT")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS componentes TEXT")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS imagen_b64 TEXT")
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
def obtener_modelo_activo():
    import google.generativeai as genai
    try:
        modelos = [m.name for m in genai.list_models() if 'generateContent' in m.supported_generation_methods]
        for m in modelos:
            if 'gemini-3.8-flash' in m: return m
        for m in modelos:
            if '3.8' in m: return m
    except Exception:
        pass
    return 'gemini-3.8-flash'

def obtener_componentes_ia(nombre_producto, api_key):
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key.strip())
        modelo_destino = obtener_modelo_activo()
        model = genai.GenerativeModel(modelo_destino)
        
        prompt = f"""Eres un experto farmacólogo veterinario. Tu única tarea es decirme los componentes activos (sustancias) del producto comercial: '{nombre_producto}'.
REGLAS ESTRICTAS:
1. Responde ÚNICAMENTE con los nombres de las sustancias separadas por comas.
2. No uses saludos, ni viñetas, ni explicaciones adicionales.
3. Haz tu mejor esfuerzo por identificarlo, es muy probable que sea una marca comercial de México o Latinoamérica.
4. SOLO si estás 100% seguro de que es un accesorio físico (gasas, jeringas) o de plano no existe, responde 'N/A'."""
        
        response = model.generate_content(prompt)
        return response.text.strip()
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "quota" in error_str.lower(): return "ERROR_CUOTA"
        elif "ModuleNotFoundError" in error_str: return "ERROR_LIBRERIA"
        return f"ERROR: {error_str}"

def recomendar_productos_ia(consulta_medico, df_catalogo, api_key):
    try:
        import google.generativeai as genai
        genai.configure(api_key=api_key.strip())
        modelo_destino = obtener_modelo_activo()
        model = genai.GenerativeModel(modelo_destino)
        
        lista_productos = []
        for _, row in df_catalogo.iterrows():
            comp = row['componentes'] if pd.notna(row['componentes']) and str(row['componentes']).strip() not in ["", "N/A"] else "Desconocido (Usa tu conocimiento farmacológico general)"
            lista_productos.append(f"{row['nombre_articulo']}: {comp}")
        
        catalogo_str = "\n".join(lista_productos)
        prompt = f"""Eres el asistente médico de la clínica Vet Playas. Inventario disponible (Producto: Componente listado en base de datos):
{catalogo_str}
Consulta del médico: "{consulta_medico}"
Regla estricta: Analiza la consulta y devuelve ÚNICAMENTE los nombres exactos de los productos que sirvan del inventario anterior, separados por el símbolo |. Si el componente de un producto dice 'Desconocido', puedes usar tu conocimiento general de farmacología veterinaria para inferir si el producto cumple con la consulta del médico. Si ningún producto del inventario sirve para la consulta, responde la palabra "NINGUNO". Sin explicaciones ni saludos."""
        
        response = model.generate_content(prompt)
        nombres = response.text.strip().split('|')
        return [n.strip() for n in nombres if n.strip()], None
    except Exception as e:
        error_str = str(e)
        if "429" in error_str or "quota" in error_str.lower(): return [], "ERROR_CUOTA"
        elif "ModuleNotFoundError" in error_str: return [], "LIBRERIA_FALTANTE"
        return [], error_str

# ==========================================
# VARIABLES DE SESIÓN Y CALLBACKS
# ==========================================
if 'carrito' not in st.session_state: st.session_state.carrito = []
if 'carrito_entradas' not in st.session_state: st.session_state.carrito_entradas = []
if 'admin_auth' not in st.session_state: st.session_state.admin_auth = False

def actualizar_componentes_desde_ia(articulo_nombre):
    if not API_KEY_GLOBAL:
        st.session_state.error_ia_edit = "Configura tu API Key."
        return
    resultado_ia = obtener_componentes_ia(articulo_nombre, API_KEY_GLOBAL)
    if resultado_ia == "ERROR_CUOTA": st.session_state.error_ia_edit = "⏳ Has consumido las consultas de hoy."
    elif "ERROR:" in resultado_ia: st.session_state.error_ia_edit = f"❌ Gemini falló: {resultado_ia}"
    else:
        st.session_state.error_ia_edit = ""
        st.session_state["input_edit_comp_val"] = resultado_ia

try:
    with open("logo.png", "rb") as f:
        st.sidebar.image(f.read(), use_container_width=True)
except FileNotFoundError:
    pass

st.sidebar.title("Navegación")
rol = st.sidebar.selectbox("Selecciona tu perfil:", ["Personal", "Administrador"])

def mostrar_tarjeta_producto(datos_articulo):
    col_img, col_info = st.columns([1, 2])
    with col_img:
        imagen_b64 = datos_articulo.get('imagen_b64', None)
        if pd.notna(imagen_b64) and str(imagen_b64).strip() != "":
            try:
                image_bytes = base64.b64decode(imagen_b64)
                st.image(image_bytes, use_container_width=True)
            except Exception:
                st.info("📷 Error al cargar la imagen")
        else:
            st.info("📷 Imagen no asignada")
    with col_info:
        st.subheader(datos_articulo['nombre_articulo'])
        if pd.notna(datos_articulo['componentes']) and datos_articulo['componentes'] != "" and datos_articulo['componentes'] != "N/A":
            st.markdown(f"🧬 **Componentes Activos:** {datos_articulo['componentes']}")
        st.write(f"**Familia:** {datos_articulo['familia']}")
        st.write(f"**Departamento:** {datos_articulo['departamento']}")
        existencia_actual = datos_articulo['existencia']
        if existencia_actual > 0: st.success(f"**Existencia Actual:** {existencia_actual} unidades disponibles.")
        else: st.error(f"**Existencia Actual:** {existencia_actual} unidades (Agotado o Pendiente de Ingreso).")

# ==========================================
# MÓDULO: PERSONAL 
# ==========================================
if rol == "Personal":
    menu_personal = st.sidebar.radio("Opciones", ["Solicitar Insumos", "Consultar Inventario"])
    
    if menu_personal == "Solicitar Insumos":
        st.title("🏥 Registro de Insumos")
        conn = get_connection()
        try:
            insumos_df = pd.read_sql_query("SELECT id_insumo, nombre_articulo FROM insumos ORDER BY nombre_articulo ASC", conn)
            personal_df = pd.read_sql_query("SELECT nombre FROM personal ORDER BY nombre ASC", conn)
        finally:
            conn.close()
            
        if insumos_df.empty or personal_df.empty:
            st.warning("⚠️ El administrador debe agregar personal y artículos al catálogo antes de poder hacer solicitudes.")
        else:
            nombres_articulos = insumos_df['nombre_articulo'].tolist()
            lista_personal = personal_df['nombre'].tolist()
            
            st.write("### 1. Datos de la Solicitud")
            col1, col2 = st.columns(2)
            area = col1.selectbox("Área", ["HOSPITALIZACIÓN", "CONSULTORIO 1", "CONSULTORIO 2", "LIMPIEZA", "RECEPCIÓN"])
            solicitante = col2.selectbox("Nombre del Solicitante", lista_personal)
            st.divider()
            st.write("### 2. Seleccionar Productos")
            with st.form("form_agregar_producto", clear_on_submit=True):
                col_a, col_b, col_c = st.columns([3, 1, 1])
                articulo_seleccionado = col_a.selectbox("Artículo", nombres_articulos)
                cantidad = col_b.number_input("Cantidad", min_value=1, value=1, step=1)
                agregar = col_c.form_submit_button("➕ Agregar al pedido")
                if agregar:
                    id_insumo = int(insumos_df.loc[insumos_df['nombre_articulo'] == articulo_seleccionado, 'id_insumo'].values[0])
                    st.session_state.carrito.append({"id_insumo": id_insumo, "nombre": articulo_seleccionado, "cantidad": cantidad})
                    st.success(f"{cantidad}x {articulo_seleccionado} agregado.")
            
            if len(st.session_state.carrito) > 0:
                st.divider()
                st.write("### 🛒 Resumen de tu Pedido")
                c_h1, c_h2, c_h3 = st.columns([4, 2, 1])
                c_h1.markdown("**Artículo**")
                c_h2.markdown("**Cantidad Pedida**")
                c_h3.markdown("**Acción**")
                st.write("---")

                for idx, item in enumerate(st.session_state.carrito):
                    c1, c2, c3 = st.columns([4, 2, 1])
                    c1.write(item['nombre'])
                    nueva_cant = c2.number_input("Cantidad", min_value=1, value=item['cantidad'], step=1, key=f"ped_cant_{idx}", label_visibility="collapsed")
                    if nueva_cant != item['cantidad']: st.session_state.carrito[idx]['cantidad'] = nueva_cant
                    if c3.button("🗑️", key=f"del_ped_{idx}", help="Eliminar de la lista"):
                        st.session_state.carrito.pop(idx)
                        st.rerun()

                st.write("---")
                col_btn1, col_btn2 = st.columns([2, 2])
                if col_btn1.button("✅ Enviar Solicitud Definitiva", type="primary"):
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
                    st.success("¡Solicitud enviada correctamente al Administrador! Limpiando pantalla...")
                    time.sleep(2) 
                    st.rerun() 
                
                if col_btn2.button("Vaciar Todo el Pedido"):
                    st.session_state.carrito = []
                    st.rerun()

    elif menu_personal == "Consultar Inventario":
        st.title("📦 Visor de Existencias")
        st.write("Consulta la disponibilidad de los insumos en tiempo real.")
        conn = get_connection()
        try:
            df_inv = pd.read_sql_query("SELECT * FROM insumos ORDER BY nombre_articulo ASC", conn)
        finally:
            conn.close()
            
        if not df_inv.empty:
            tab_b1, tab_b2 = st.tabs(["🔍 Búsqueda Normal", "🤖 Asistente Médico (IA)"])
            with tab_b1:
                st.write("Busca seleccionando el nombre comercial de la lista.")
                articulo_buscar = st.selectbox("Selecciona el artículo:", [""] + df_inv['nombre_articulo'].tolist())
                if articulo_buscar:
                    datos_articulo = df_inv[df_inv['nombre_articulo'] == articulo_buscar].iloc[0]
                    mostrar_tarjeta_producto(datos_articulo)
            with tab_b2:
                st.write("Escribe lo que necesitas y la IA te recomendará productos.")
                consulta_medico = st.text_input("Ejemplo: 'Algo para desparasitar perros', 'Meloxicam'")
                if st.button("Consultar a Gemini ✨", type="primary"):
                    if not API_KEY_GLOBAL: st.error("⚠️️ La inteligencia artificial no ha sido configurada.")
                    elif not consulta_medico: st.warning("Por favor escribe tu consulta.")
                    else:
                        with st.spinner("🧠 Analizando el catálogo..."):
                            resultados_ia, error_ia = recomendar_productos_ia(consulta_medico, df_inv, API_KEY_GLOBAL)
                            if error_ia == "ERROR_CUOTA": st.error("⏳ Has alcanzado el límite de consultas gratuitas de Google por hoy.")
                            elif error_ia == "LIBRERIA_FALTANTE": st.error("❌ Falta la librería. Ejecuta: pip install google-generativeai")
                            elif error_ia: st.error(f"❌ Error de Gemini: {error_ia}")
                            elif not resultados_ia or "NINGUNO" in [r.upper() for r in resultados_ia]: st.warning("La IA no encontró ningún producto.")
                            else:
                                st.success("Gemini recomienda los siguientes productos:")
                                for nombre_sugerido in resultados_ia:
                                    match = df_inv[df_inv['nombre_articulo'].str.upper() == nombre_sugerido.upper()]
                                    if not match.empty:
                                        st.write("---")
                                        mostrar_tarjeta_producto(match.iloc[0])
        else:
            st.info("El catálogo está vacío.")

# ==========================================
# MÓDULO: ADMINISTRADOR
# ==========================================
elif rol == "Administrador":
    
    if not st.session_state.admin_auth:
        st.title("🔒 Acceso Restringido")
        with st.form("form_login"):
            pwd_ingresada = st.text_input("Contraseña", type="password")
            submit_login = st.form_submit_button("Ingresar")
            if submit_login:
                if pwd_ingresada == "vetplayas": 
                    st.session_state.admin_auth = True
                    st.success("Acceso concedido.")
                    time.sleep(1)
                    st.rerun()
                else: st.error("❌ Contraseña incorrecta. Intenta de nuevo.")
    else:
        st.sidebar.markdown("---")
        if st.sidebar.button("🔴 Cerrar Sesión Admin"):
            st.session_state.admin_auth = False
            st.rerun()
            
        st.title("⚙ Panel de Administración")
        menu_admin = st.sidebar.radio("Opciones de Administrador", ["Surtir Solicitudes", "Entradas / Compras", "Subir Excel de Ventas", "Reportes y Exportación", "Catálogo de Insumos", "Gestión de Personal", "Configuración IA"])
        
        if menu_admin == "Surtir Solicitudes":
            st.subheader("📦 Solicitudes Pendientes de Surtir")
            conn = get_connection()
            try:
                personal_df = pd.read_sql_query("SELECT nombre FROM personal ORDER BY nombre ASC", conn)
                lista_personal = [""] + personal_df['nombre'].tolist() if not personal_df.empty else [""]
                solicitudes_df = pd.read_sql_query("SELECT * FROM solicitudes WHERE UPPER(estado) = 'PENDIENTE' ORDER BY fecha_hora ASC", conn)
                if solicitudes_df.empty:
                    st.info("Todo al día. No hay solicitudes pendientes.")
                else:
                    for index, row in solicitudes_df.iterrows():
                        id_sol = row['id_solicitud']
                        with st.expander(f"🔴 Solicitud #{id_sol} - {row['solicitante']} ({row['area']}) - {row['fecha_hora']}"):
                            detalles_df = pd.read_sql_query(f"SELECT d.id_detalle, d.cantidad_pedida, i.nombre_articulo, i.id_insumo FROM detalle_solicitud d JOIN insumos i ON d.id_insumo = i.id_insumo WHERE d.id_solicitud = {id_sol}", conn)
                            with st.form(key=f"form_surtir_{id_sol}"):
                                quien_surte = st.selectbox("¿Quién entrega el material?", lista_personal, key=f"quien_{id_sol}")
                                cantidades_a_entregar = {}
                                for _, det_row in detalles_df.iterrows():
                                    c1_i, c2_i, c3_i = st.columns([3, 1, 1.5])
                                    c1_i.write(det_row['nombre_articulo'])
                                    c2_i.write(str(det_row['cantidad_pedida']))
                                    cantidades_a_entregar[det_row['id_detalle']] = c3_i.number_input("Entregar", min_value=0, value=int(det_row['cantidad_pedida']), step=1, key=f"entregar_{det_row['id_detalle']}", label_visibility="collapsed")
                                submit_surtir = st.form_submit_button(f"✅ Autorizar y Surtir Solicitud #{id_sol}")
                                if submit_surtir:
                                    if not quien_surte: st.error("⚠️ Error: Selecciona a la persona que entrega.")
                                    else:
                                        c = conn.cursor()
                                        c.execute("UPDATE solicitudes SET estado = 'SURTIDA', quien_surte = %s WHERE id_solicitud = %s", (quien_surte, id_sol))
                                        for idx_row, d_row in detalles_df.iterrows():
                                            cant_entregada = cantidades_a_entregar[d_row['id_detalle']]
                                            c.execute("UPDATE detalle_solicitud SET cantidad_entregada = %s WHERE id_detalle = %s", (cant_entregada, d_row['id_detalle']))
                                            c.execute("UPDATE insumos SET existencia = existencia - %s WHERE id_insumo = %s", (cant_entregada, d_row['id_insumo']))
                                        conn.commit()
                                        st.success(f"Solicitud #{id_sol} surtida.")
                                        time.sleep(1.5)
                                        st.rerun()
            finally:
                conn.close()

        elif menu_admin == "Entradas / Compras":
            st.subheader("📥 Registrar Ingreso (Factura o Conteo)")
            tab1, tab2 = st.tabs(["📦 Agregar Artículo Existente", "🆕 Crear Artículo y Agregar"])
            conn = get_connection()
            try:
                insumos_df = pd.read_sql_query("SELECT id_insumo, nombre_articulo FROM insumos ORDER BY nombre_articulo ASC", conn)
                nombres_articulos = insumos_df['nombre_articulo'].tolist() if not insumos_df.empty else []
                with tab1:
                    if nombres_articulos:
                        with st.form("form_entradas_existente", clear_on_submit=True):
                            articulo = st.selectbox("Selecciona el Artículo", nombres_articulos)
                            col1, col2 = st.columns(2)
                            cantidad_cajas = col1.number_input("Cantidad Recibida (Cajas)", min_value=1, value=1, step=1)
                            piezas_por_caja = col2.number_input("Piezas por Caja", min_value=1, value=1, step=1)
                            if st.form_submit_button("➕ Agregar a la Factura"):
                                id_insumo = int(insumos_df.loc[insumos_df['nombre_articulo'] == articulo, 'id_insumo'].values[0])
                                st.session_state.carrito_entradas.append({"id_insumo": id_insumo, "nombre": articulo, "cajas": cantidad_cajas, "piezas_caja": piezas_por_caja, "total": cantidad_cajas * piezas_por_caja})
                                st.success(f"Agregado.")
                                time.sleep(1)
                                st.rerun()
                    else: st.warning("No hay artículos.")
                with tab2:
                    with st.form("form_entradas_nuevo", clear_on_submit=True):
                        col_n1, col_n2, col_n3 = st.columns(3)
                        nombre_nuevo = col_n1.text_input("Nombre del Artículo")
                        familia_nueva = col_n2.selectbox("Familia", ["PRODUCTOS", "SERVICIOS"])
                        departamento_nuevo = col_n3.selectbox("Departamento", ["EXAMENES", "SERVICIOS", "MEDICAMENTO", "ALIMENTOS", "VACUNAS", "ROPA", "ACCESORIOS"])
                        componentes_nuevos = st.text_input("Componentes Activos (Opcional)")
                        col_c1, col_c2 = st.columns(2)
                        cantidad_cajas_nueva = col_c1.number_input("Cajas / Paquetes", min_value=1, value=1, step=1)
                        piezas_por_caja_nueva = col_c2.number_input("Piezas por Caja", min_value=1, value=1, step=1)
                        if st.form_submit_button("Crear y Agregar a la Factura") and nombre_nuevo:
                            nombre_mayus = nombre_nuevo.strip().upper()
                            try:
                                c = conn.cursor()
                                c.execute("INSERT INTO insumos (nombre_articulo, familia, departamento, componentes, existencia) VALUES (%s, %s, %s, %s, 0) RETURNING id_insumo", (nombre_mayus, familia_nueva, departamento_nuevo, componentes_nuevos))
                                id_insumo_creado = c.fetchone()[0]
                                conn.commit()
                                st.session_state.carrito_entradas.append({"id_insumo": id_insumo_creado, "nombre": nombre_mayus, "cajas": cantidad_cajas_nueva, "piezas_caja": piezas_por_caja_nueva, "total": cantidad_cajas_nueva * piezas_por_caja_nueva})
                                st.success("Creado y agregado.")
                                time.sleep(1)
                                st.rerun()
                            except IntegrityError: st.error("⚠️ Este artículo ya existe.")

                if len(st.session_state.carrito_entradas) > 0:
                    st.divider()
                    st.write("### 🛒 Resumen de Factura")
                    for idx, item in enumerate(st.session_state.carrito_entradas):
                        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])
                        c1.write(item['nombre'])
                        nuevas_cajas = c2.number_input("Cajas", min_value=1, value=item['cajas'], step=1, key=f"ent_caja_{idx}", label_visibility="collapsed")
                        nuevas_pzas = c3.number_input("Pzas", min_value=1, value=item['piezas_caja'], step=1, key=f"ent_pza_{idx}", label_visibility="collapsed")
                        if nuevas_cajas != item['cajas'] or nuevas_pzas != item['piezas_caja']:
                            st.session_state.carrito_entradas[idx]['cajas'] = nuevas_cajas
                            st.session_state.carrito_entradas[idx]['piezas_caja'] = nuevas_pzas
                            st.session_state.carrito_entradas[idx]['total'] = nuevas_cajas * nuevas_pzas
                            st.rerun() 
                        c4.markdown(f"<span style='color:#1f77b4; font-weight:bold;'>{item['total']}</span>", unsafe_allow_html=True)
                        if c5.button("🗑️", key=f"del_ent_{idx}"):
                            st.session_state.carrito_entradas.pop(idx)
                            st.rerun()

                    st.write("---")
                    tipo_movimiento = st.selectbox("Tipo de Ingreso", ["Compra / Factura", "Ajuste por Conteo Físico"])
                    comentarios = st.text_input("Comentarios (Ej. F-10294)")
                    archivo_factura = st.file_uploader("Adjuntar Archivo", type=["pdf", "png", "jpg", "jpeg"])
                    
                    col_btn1, col_btn2 = st.columns([2, 2])
                    if col_btn1.button("✅ Guardar Factura", type="primary"):
                        info_archivo_guardado = ""
                        if archivo_factura is not None:
                            nombre_archivo_limpio = f"{datetime.now().strftime('%Y%m%d%H%M%S')}_{archivo_factura.name}"
                            with open(os.path.join("facturas", nombre_archivo_limpio), "wb") as f: f.write(archivo_factura.getbuffer())
                            info_archivo_guardado = f" | Archivo: {nombre_archivo_limpio}"
                        c = conn.cursor()
                        fecha_actual = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                        for item in st.session_state.carrito_entradas:
                            detalle = f"{item['cajas']} caja(s) de {item['piezas_caja']}. Ref: {comentarios}{info_archivo_guardado}"
                            c.execute("INSERT INTO historial_entradas (fecha_hora, id_insumo, cantidad_agregada, tipo_movimiento, comentarios) VALUES (%s, %s, %s, %s, %s)", (fecha_actual, item['id_insumo'], item['total'], tipo_movimiento, detalle))
                            c.execute("UPDATE insumos SET existencia = existencia + %s WHERE id_insumo = %s", (item['total'], item['id_insumo']))
                        conn.commit()
                        st.session_state.carrito_entradas = []
                        st.success("🎉 Factura guardada.")
                        time.sleep(2)
                        st.rerun()
                    if col_btn2.button("Vaciar Factura y Cancelar"):
                        st.session_state.carrito_entradas = []
                        st.rerun()
            finally:
                conn.close()

        elif menu_admin == "Subir Excel de Ventas":
            st.subheader("📉 Descuento Masivo por Ventas")
            archivo_excel = st.file_uploader("Cargar reporte", type=["xls", "xlsx"])
            if archivo_excel is not None:
                try:
                    df_ventas = pd.read_excel(archivo_excel)
                    df_ventas.columns = [str(col).strip().upper() for col in df_ventas.columns]
                    if 'PRODUCTO' in df_ventas.columns and 'VOLUMEN' in df_ventas.columns:
                        if st.button("Aplicar Descuentos", type="primary"):
                            conn = get_connection()
                            try:
                                c = conn.cursor()
                                count_ok = 0
                                count_fail = []
                                for _, row in df_ventas.iterrows():
                                    prod = str(row['PRODUCTO']).strip().upper()
                                    try: vol = int(float(row['VOLUMEN']))
                                    except: vol = 0
                                    if pd.notna(prod) and vol > 0 and prod != "NAN":
                                        c.execute("SELECT id_insumo FROM insumos WHERE nombre_articulo = %s", (prod,))
                                        res = c.fetchone()
                                        if res:
                                            c.execute("UPDATE insumos SET existencia = existencia - %s WHERE id_insumo = %s", (vol, res[0]))
                                            count_ok += 1
                                        else: count_fail.append(prod)
                                conn.commit()
                                st.success(f"✅ Se descontaron {count_ok} productos.")
                                if count_fail: st.warning(f"⚠️ {len(count_fail)} no encontrados.")
                            finally:
                                conn.close()
                    else: st.error("❌ Falta columna 'PRODUCTO' o 'VOLUMEN'.")
                except Exception as e: st.error(f"Error: {e}")

        elif menu_admin == "Reportes y Exportación":
            st.subheader("📊 Descarga de Reportes")
            conn = get_connection()
            try:
                df_inventario = pd.read_sql_query("SELECT nombre_articulo as Artículo, componentes as Componentes, familia as Familia, departamento as Departamento, existencia as Existencia FROM insumos ORDER BY nombre_articulo ASC", conn)
                df_solicitudes = pd.read_sql_query("SELECT s.id_solicitud as Folio, s.fecha_hora as Fecha, s.area as Área, s.solicitante as Solicitante, s.estado as Estado, s.quien_surte as Autorizó, i.nombre_articulo as Artículo, d.cantidad_pedida as Pedido, d.cantidad_entregada as Entregado FROM solicitudes s JOIN detalle_solicitud d ON s.id_solicitud = d.id_solicitud JOIN insumos i ON d.id_insumo = i.id_insumo ORDER BY s.fecha_hora DESC", conn)
                df_entradas = pd.read_sql_query("SELECT h.fecha_hora as Fecha, i.nombre_articulo as Artículo, h.cantidad_agregada as Cantidad_Ingresada, h.tipo_movimiento as Movimiento, h.comentarios as Comentarios FROM historial_entradas h JOIN insumos i ON h.id_insumo = i.id_insumo ORDER BY h.fecha_hora DESC", conn)
            finally:
                conn.close()

            def convert_df(df):
                output = io.BytesIO()
                with pd.ExcelWriter(output, engine='openpyxl') as w: df.to_excel(w, index=False)
                return output.getvalue()
                
            c1, c2, c3 = st.columns(3)
            if not df_inventario.empty: c1.download_button("📥 Inventario", data=convert_df(df_inventario), file_name="Inventario.xlsx")
            if not df_solicitudes.empty: c2.download_button("📥 Salidas", data=convert_df(df_solicitudes), file_name="Salidas.xlsx")
            if not df_entradas.empty: c3.download_button("📥 Entradas", data=convert_df(df_entradas), file_name="Entradas.xlsx")

        elif menu_admin == "Catálogo de Insumos":
            st.subheader("Catálogo de Insumos")
            tab_cat1, tab_cat2, tab_cat3 = st.tabs(["📦 Crear", "📸 Imagen", "🧪 Componentes"])
            with tab_cat1:
                col1, col2, col3 = st.columns(3)
                nombre = col1.text_input("Nombre", key="n_art")
                familia = col2.selectbox("Familia", ["PRODUCTOS", "SERVICIOS"], key="f_art")
                departamento = col3.selectbox("Departamento", ["EXAMENES", "SERVICIOS", "MEDICAMENTO", "ALIMENTOS", "VACUNAS", "ROPA", "ACCESORIOS"], key="d_art")
                
                if "n_comp" not in st.session_state: st.session_state["n_comp"] = ""
                cb1, cb2 = st.columns([1, 2])
                with cb1:
                    st.write("")
                    if st.button("✨ Autocompletar con IA", key="ia_crear"):
                        if not API_KEY_GLOBAL: st.error("Configura tu API Key.")
                        elif nombre:
                            with st.spinner("Investigando..."):
                                res = obtener_componentes_ia(nombre, API_KEY_GLOBAL)
                                if "ERROR" in res: st.error(res)
                                else: st.session_state["n_comp"] = res
                        else: st.warning("Escribe el nombre.")
                componentes = cb2.text_input("Componentes", value=st.session_state.get("n_comp", ""), key="n_comp_in")
                if st.button("Guardar en Catálogo", type="primary"):
                    if nombre:
                        conn = get_connection()
                        try:
                            c = conn.cursor()
                            c.execute("INSERT INTO insumos (nombre_articulo, familia, departamento, componentes, existencia) VALUES (%s, %s, %s, %s, 0)", (nombre.strip().upper(), familia, departamento, componentes))
                            conn.commit()
                            st.session_state["n_comp"] = "" 
                            st.success("Agregado exitosamente.")
                            time.sleep(1.5)
                            st.rerun()
                        except IntegrityError: st.error("Ya existe.")
                        finally: conn.close()
                    else: st.error("Nombre obligatorio.")
                            
            with tab_cat2:
                conn = get_connection()
                try: insumos_df_img = pd.read_sql_query("SELECT id_insumo, nombre_articulo, imagen_b64 FROM insumos ORDER BY nombre_articulo ASC", conn)
                finally: conn.close()
                if not insumos_df_img.empty:
                    articulo_img = st.selectbox("Selecciona artículo", insumos_df_img['nombre_articulo'].tolist(), key="sel_img")
                    datos_art = insumos_df_img[insumos_df_img['nombre_articulo'] == articulo_img].iloc[0]
                    img_b64 = datos_art.get('imagen_b64', None)
                    if pd.notna(img_b64) and str(img_b64).strip() != "":
                        try: st.image(base64.b64decode(img_b64), width=250)
                        except: pass
                    with st.form("f_foto", clear_on_submit=True):
                        archivo_img = st.file_uploader("Sube foto", type=["jpg", "png", "jpeg"])
                        if st.form_submit_button("Guardar Imagen") and archivo_img:
                            conn = get_connection()
                            try:
                                c = conn.cursor()
                                c.execute("UPDATE insumos SET imagen_b64 = %s WHERE id_insumo = %s", (base64.b64encode(archivo_img.getvalue()).decode(), int(datos_art['id_insumo'])))
                                conn.commit()
                                st.success("Imagen actualizada.")
                                time.sleep(1.5)
                                st.rerun()
                            finally: conn.close()

            with tab_cat3:
                conn = get_connection()
                try: insumos_df_comp = pd.read_sql_query("SELECT id_insumo, nombre_articulo, componentes FROM insumos ORDER BY nombre_articulo ASC", conn)
                finally: conn.close()
                if not insumos_df_comp.empty:
                    art_comp = st.selectbox("Selecciona artículo:", insumos_df_comp['nombre_articulo'].tolist(), key="sel_comp")
                    if "last_art" not in st.session_state or st.session_state["last_art"] != art_comp:
                        datos_comp = insumos_df_comp[insumos_df_comp['nombre_articulo'] == art_comp].iloc[0]
                        st.session_state["in_comp"] = datos_comp.get('componentes', "") if pd.notna(datos_comp.get('componentes')) else ""
                        st.session_state["last_art"] = art_comp
                        st.session_state.err_ia = ""
                    cb_ia, cb_txt = st.columns([1, 2])
                    with cb_ia:
                        st.write("")
                        st.button("✨ Investigar", key="ia_ed", on_click=actualizar_componentes_desde_ia, args=(art_comp,))
                        if st.session_state.get("error_ia_edit", ""): st.error(st.session_state.error_ia_edit)
                    n_comp = cb_txt.text_input("Componentes", value=st.session_state.get("input_edit_comp_val", st.session_state["in_comp"]), key="input_edit_comp_val")
                    if st.button("Actualizar Componentes", type="primary"):
                        conn = get_connection()
                        try:
                            c = conn.cursor()
                            c.execute("UPDATE insumos SET componentes = %s WHERE id_insumo = %s", (n_comp, int(insumos_df_comp[insumos_df_comp['nombre_articulo'] == art_comp].iloc[0]['id_insumo'])))
                            conn.commit()
                            st.success("Actualizado.")
                            time.sleep(1.5)
                            st.rerun()
                        finally: conn.close()
                        
        elif menu_admin == "Gestión de Personal":
            st.subheader("Agregar Colaborador")
            with st.form("f_pers", clear_on_submit=True):
                n_pers = st.text_input("Nombre completo")
                if st.form_submit_button("Guardar Nombre") and n_pers:
                    conn = get_connection()
                    try:
                        c = conn.cursor()
                        c.execute("INSERT INTO personal (nombre) VALUES (%s)", (n_pers.strip().upper(),))
                        conn.commit()
                        st.success("Registrado.")
                    except IntegrityError: st.error("Ya registrado.")
                    finally: conn.close()
            st.write("---")
            conn = get_connection()
            try: st.dataframe(pd.read_sql_query("SELECT * FROM personal", conn), use_container_width=True)
            finally: conn.close()
            
        elif menu_admin == "Configuración IA":
            st.subheader("🤖 Configuración de Gemini AI")
            with st.form("f_ia"):
                n_llave = st.text_input("Gemini API Key:", value=API_KEY_GLOBAL, type="password")
                if st.form_submit_button("Guardar Configuración"):
                    conn = get_connection()
                    try:
                        c = conn.cursor()
                        c.execute("INSERT INTO configuracion (parametro, valor) VALUES (%s, %s) ON CONFLICT (parametro) DO UPDATE SET valor = EXCLUDED.valor", ('gemini_api_key', n_llave.strip()))
                        conn.commit()
                        st.success("Guardado.")
                        time.sleep(1.5)
                        st.rerun()
                    finally: conn.close()
