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

# ==========================================
# Cargar el ícono cuadrado (Asegúrate de tener icono_app.png en GitHub)
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
        
        # Migraciones (Incluyendo Min y Max para Compras)
        c.execute("ALTER TABLE solicitudes ADD COLUMN IF NOT EXISTS quien_surte TEXT")
        c.execute("ALTER TABLE detalle_solicitud ADD COLUMN IF NOT EXISTS cantidad_entregada INTEGER")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS ruta_imagen TEXT")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS componentes TEXT")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS imagen_b64 TEXT")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS stock_minimo INTEGER DEFAULT 0")
        c.execute("ALTER TABLE insumos ADD COLUMN IF NOT EXISTS stock_maximo INTEGER DEFAULT 0")
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
3. Haz tu mejor esfuerzo por identificarlo.
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
            comp = row['componentes'] if pd.notna(row['componentes']) and str(row['componentes']).strip() not in ["", "N/A"] else "Desconocido (Usa tu conocimiento general)"
            lista_productos.append(f"{row['nombre_articulo']}: {comp}")
        
        catalogo_str = "\n".join(lista_productos)
        prompt = f"""Eres el asistente médico de la clínica Vet Playas. Inventario disponible:
{catalogo_str}
Consulta del médico: "{consulta_medico}"
Regla estricta: Analiza la consulta y devuelve ÚNICAMENTE los nombres exactos de los productos que sirvan del inventario anterior, separados por el símbolo |. Si ningún producto sirve, responde "NINGUNO". Sin explicaciones ni saludos."""
        
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
    if resultado_ia == "ERROR_CUOTA": st.session_state.error_ia_edit = "⏳ Límite alcanzado."
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
                st.image(base64.b64decode(imagen_b64), use_container_width=True)
            except Exception:
                st.info("📷 Error de imagen")
        else:
            st.info("📷 Imagen no asignada")
    with col_info:
        st.subheader(datos_articulo['nombre_articulo'])
        if pd.notna(datos_articulo['componentes']) and datos_articulo['componentes'] not in ["", "N/A"]:
            st.markdown(f"🧬 **Componentes:** {datos_articulo['componentes']}")
        st.write(f"**Familia:** {datos_articulo['familia']} | **Departamento:** {datos_articulo['departamento']}")
        
        ex = datos_articulo['existencia']
        s_min = datos_articulo.get('stock_minimo', 0)
        s_max = datos_articulo.get('stock_maximo', 0)
        
        if ex > s_min: 
            st.success(f"**Existencia Actual:** {ex} unidades (Óptimo)")
        elif ex > 0 and ex <= s_min:
            st.warning(f"**Existencia Actual:** {ex} unidades (Alerta: Stock Mínimo alcanzado)")
        else: 
            st.error(f"**Existencia Actual:** {ex} unidades (Agotado)")

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
            st.warning("⚠️ El administrador debe agregar personal y artículos al catálogo.")
        else:
            st.write("### 1. Datos de la Solicitud")
            col1, col2 = st.columns(2)
            area = col1.selectbox("Área", ["HOSPITALIZACIÓN", "CONSULTORIO 1", "CONSULTORIO 2", "LIMPIEZA", "RECEPCIÓN"])
            solicitante = col2.selectbox("Nombre del Solicitante", personal_df['nombre'].tolist())
            st.divider()
            
            with st.form("form_agregar_producto", clear_on_submit=True):
                col_a, col_b, col_c = st.columns([3, 1, 1])
                articulo_seleccionado = col_a.selectbox("Artículo", insumos_df['nombre_articulo'].tolist())
                cantidad = col_b.number_input("Cantidad", min_value=1, value=1, step=1)
                if col_c.form_submit_button("➕ Agregar"):
                    id_insumo = int(insumos_df.loc[insumos_df['nombre_articulo'] == articulo_seleccionado, 'id_insumo'].values[0])
                    st.session_state.carrito.append({"id_insumo": id_insumo, "nombre": articulo_seleccionado, "cantidad": cantidad})
                    st.success(f"{cantidad}x {articulo_seleccionado} agregado.")
            
            if len(st.session_state.carrito) > 0:
                st.divider()
                st.write("### 🛒 Resumen de tu Pedido")
                for idx, item in enumerate(st.session_state.carrito):
                    c1, c2, c3 = st.columns([4, 2, 1])
                    c1.write(item['nombre'])
                    nueva_cant = c2.number_input("Cantidad", min_value=1, value=item['cantidad'], step=1, key=f"ped_cant_{idx}", label_visibility="collapsed")
                    if nueva_cant != item['cantidad']: st.session_state.carrito[idx]['cantidad'] = nueva_cant
                    if c3.button("🗑️", key=f"del_ped_{idx}"):
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
                    st.success("¡Solicitud enviada!")
                    time.sleep(2) 
                    st.rerun() 
                
                if col_btn2.button("Vaciar Todo"):
                    st.session_state.carrito = []
                    st.rerun()

elif menu_personal == "Consultar Inventario":
        st.title("📦 Visor de Existencias")
        conn = get_connection()
        try: df_inv = pd.read_sql_query("SELECT * FROM insumos ORDER BY nombre_articulo ASC", conn)
        finally: conn.close()
            
        if not df_inv.empty:
            tab_b1, tab_b2 = st.tabs(["🔍 Búsqueda Normal", "🤖 Asistente Médico (IA)"])
            with tab_b1:
                st.write("**Buscador de Detalles:**")
                articulo_buscar = st.selectbox("Selecciona un artículo para ver su foto y detalles específicos:", [""] + df_inv['nombre_articulo'].tolist())
                if articulo_buscar:
                    mostrar_tarjeta_producto(df_inv[df_inv['nombre_articulo'] == articulo_buscar].iloc[0])
                
                st.write("---")
                st.write("### 📋 Directorio General")
                # Creamos la tabla limpia que pediste
                df_mostrar = df_inv[['nombre_articulo', 'componentes', 'existencia']].copy()
                df_mostrar['Imagen'] = df_inv['imagen_b64'].apply(lambda x: "✅" if pd.notna(x) and str(x).strip() != "" else "🚫")
                df_mostrar.columns = ['Producto', 'Componentes', 'Existencia', 'Imagen']
                st.dataframe(df_mostrar, use_container_width=True, hide_index=True)
                
            with tab_b2:
                consulta_medico = st.text_input("Consulta a la IA (Ej: 'Antibiótico en suspensión')")
                if st.button("Consultar ✨", type="primary"):
                    if not API_KEY_GLOBAL: st.error("⚠ IA no configurada.")
                    elif not consulta_medico: st.warning("Escribe tu consulta.")
                    else:
                        with st.spinner("Analizando..."):
                            resultados_ia, error_ia = recomendar_productos_ia(consulta_medico, df_inv, API_KEY_GLOBAL)
                            if error_ia: st.error(f"❌ Error: {error_ia}")
                            elif not resultados_ia or "NINGUNO" in [r.upper() for r in resultados_ia]: st.warning("No se encontró nada.")
                            else:
                                for nombre_sugerido in resultados_ia:
                                    match = df_inv[df_inv['nombre_articulo'].str.upper() == nombre_sugerido.upper()]
                                    if not match.empty:
                                        st.write("---")
                                        mostrar_tarjeta_producto(match.iloc[0])
        else: st.info("Catálogo vacío.")

# ==========================================
# MÓDULO: ADMINISTRADOR
# ==========================================
elif rol == "Administrador":
    
    if not st.session_state.admin_auth:
        st.title("🔒 Acceso Restringido")
        with st.form("form_login"):
            # Separamos la caja de texto y el botón para que se dibujen bien
            pwd_ingresada = st.text_input("Contraseña", type="password")
            submit_login = st.form_submit_button("Ingresar")
            
            if submit_login:
                if pwd_ingresada == "vetplayas": 
                    st.session_state.admin_auth = True
                    st.rerun()
                else: 
                    st.error("❌ Contraseña incorrecta. Intenta de nuevo.")
    else:
        st.sidebar.markdown("---")
        if st.sidebar.button("🔴 Cerrar Sesión"):
            st.session_state.admin_auth = False
            st.rerun()
            
        st.title("⚙ Panel de Administración")
        menu_admin = st.sidebar.radio("Opciones", ["Surtir Solicitudes", "Entradas / Compras", "Subir Excel de Ventas", "Reportes y Exportación", "Catálogo de Insumos", "Gestión de Personal", "Configuración IA"])
        
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
                        with st.expander(f"🔴 Solicitud #{id_sol} - {row['solicitante']} - {row['fecha_hora']}"):
                            detalles_df = pd.read_sql_query(f"SELECT d.id_detalle, d.cantidad_pedida, i.nombre_articulo, i.id_insumo FROM detalle_solicitud d JOIN insumos i ON d.id_insumo = i.id_insumo WHERE d.id_solicitud = {id_sol}", conn)
                            with st.form(key=f"form_surtir_{id_sol}"):
                                quien_surte = st.selectbox("¿Quién entrega?", lista_personal, key=f"quien_{id_sol}")
                                cantidades_a_entregar = {}
                                for _, det_row in detalles_df.iterrows():
                                    c1_i, c2_i, c3_i = st.columns([3, 1, 1.5])
                                    c1_i.write(det_row['nombre_articulo'])
                                    c2_i.write(str(det_row['cantidad_pedida']))
                                    cantidades_a_entregar[det_row['id_detalle']] = c3_i.number_input("Entregar", min_value=0, value=int(det_row['cantidad_pedida']), step=1, label_visibility="collapsed")
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

        elif menu_admin == "Entradas / Compras":
            st.subheader("📥 Registrar Ingreso")
            tab1, tab2 = st.tabs(["📦 Existente", "🆕 Nuevo"])
            conn = get_connection()
            try:
                insumos_df = pd.read_sql_query("SELECT id_insumo, nombre_articulo FROM insumos ORDER BY nombre_articulo ASC", conn)
                with tab1:
                    with st.form("form_existente", clear_on_submit=True):
                        articulo = st.selectbox("Artículo", insumos_df['nombre_articulo'].tolist() if not insumos_df.empty else [])
                        col1, col2 = st.columns(2)
                        cajas = col1.number_input("Cajas", min_value=1, value=1)
                        pzas = col2.number_input("Piezas x Caja", min_value=1, value=1)
                        if st.form_submit_button("➕ Agregar") and articulo:
                            id_ins = int(insumos_df.loc[insumos_df['nombre_articulo'] == articulo, 'id_insumo'].values[0])
                            st.session_state.carrito_entradas.append({"id_insumo": id_ins, "nombre": articulo, "cajas": cajas, "piezas_caja": pzas, "total": cajas * pzas})
                            st.rerun()
                with tab2:
                    with st.form("form_nuevo", clear_on_submit=True):
                        col_n1, col_n2, col_n3 = st.columns(3)
                        n_nuevo = col_n1.text_input("Nombre")
                        f_nueva = col_n2.selectbox("Familia", ["PRODUCTOS", "SERVICIOS"])
                        d_nuevo = col_n3.selectbox("Depto", ["EXAMENES", "SERVICIOS", "MEDICAMENTO", "ALIMENTOS", "VACUNAS", "ROPA", "ACCESORIOS"])
                        comp_nuevo = st.text_input("Componentes")
                        
                        col_m1, col_m2 = st.columns(2)
                        s_min = col_m1.number_input("Stock Mínimo", min_value=0, value=2)
                        s_max = col_m2.number_input("Stock Máximo", min_value=1, value=10)
                        
                        col_c1, col_c2 = st.columns(2)
                        c_n = col_c1.number_input("Cajas", min_value=1, value=1)
                        p_n = col_c2.number_input("Pzas x Caja", min_value=1, value=1)
                        if st.form_submit_button("Crear y Agregar") and n_nuevo:
                            try:
                                c = conn.cursor()
                                c.execute("INSERT INTO insumos (nombre_articulo, familia, departamento, componentes, existencia, stock_minimo, stock_maximo) VALUES (%s, %s, %s, %s, 0, %s, %s) RETURNING id_insumo", (n_nuevo.strip().upper(), f_nueva, d_nuevo, comp_nuevo, s_min, s_max))
                                id_creado = c.fetchone()[0]
                                conn.commit()
                                st.session_state.carrito_entradas.append({"id_insumo": id_creado, "nombre": n_nuevo.upper(), "cajas": c_n, "piezas_caja": p_n, "total": c_n * p_n})
                                st.rerun()
                            except IntegrityError: st.error("Ya existe.")

                if st.session_state.carrito_entradas:
                    st.divider()
                    for idx, item in enumerate(st.session_state.carrito_entradas):
                        c1, c2, c3, c4, c5 = st.columns([3, 2, 2, 2, 1])
                        c1.write(item['nombre'])
                        n_c = c2.number_input("Cajas", min_value=1, value=item['cajas'], key=f"ec_{idx}", label_visibility="collapsed")
                        n_p = c3.number_input("Pzas", min_value=1, value=item['piezas_caja'], key=f"ep_{idx}", label_visibility="collapsed")
                        if n_c != item['cajas'] or n_p != item['piezas_caja']:
                            st.session_state.carrito_entradas[idx].update({"cajas": n_c, "piezas_caja": n_p, "total": n_c * n_p})
                            st.rerun()
                        c4.write(f"**{item['total']}**")
                        if c5.button("🗑️", key=f"de_{idx}"): st.session_state.carrito_entradas.pop(idx); st.rerun()

                    tipo = st.selectbox("Tipo", ["Compra / Factura", "Ajuste"])
                    coment = st.text_input("Folio Factura")
                    arch = st.file_uploader("Adjuntar", type=["pdf", "png", "jpg"])
                    if st.button("✅ Guardar Factura", type="primary"):
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
                        conn.commit()
                        st.session_state.carrito_entradas = []
                        st.success("Guardado.")
                        time.sleep(1); st.rerun()
            finally: conn.close()

        elif menu_admin == "Subir Excel de Ventas":
            st.subheader("📉 Descuento Masivo")
            arch = st.file_uploader("Reporte", type=["xls", "xlsx"])
            if arch:
                df_v = pd.read_excel(arch)
                df_v.columns = [str(c).strip().upper() for c in df_v.columns]
                if 'PRODUCTO' in df_v.columns and 'VOLUMEN' in df_v.columns:
                    if st.button("Aplicar Descuentos", type="primary"):
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
                            st.success(f"✅ {ok} descontados.")
                        finally: conn.close()
                else: st.error("Faltan columnas.")

        elif menu_admin == "Reportes y Exportación":
            st.subheader("📊 Reportes y Compras")
            conn = get_connection()
            try:
                # 🔴 MOTOR DE COMPRAS (SUGERENCIA AUTOMÁTICA)
                df_compras = pd.read_sql_query("""
                    SELECT nombre_articulo as "Artículo", familia as "Familia", departamento as "Departamento",
                           existencia as "Stock_Actual", stock_minimo as "Mínimo", stock_maximo as "Máximo",
                           (stock_maximo - existencia) as "Cantidad_A_Pedir"
                    FROM insumos 
                    WHERE existencia <= stock_minimo
                    ORDER BY departamento ASC, nombre_articulo ASC
                """, conn)
                
                df_inv = pd.read_sql_query("SELECT nombre_articulo, existencia, stock_minimo, stock_maximo FROM insumos ORDER BY nombre_articulo ASC", conn)
                df_sol = pd.read_sql_query("SELECT s.id_solicitud as Folio, s.fecha_hora as Fecha, s.area as Área, s.solicitante as Solicitante, s.estado as Estado, i.nombre_articulo as Artículo, d.cantidad_entregada as Entregado FROM solicitudes s JOIN detalle_solicitud d ON s.id_solicitud = d.id_solicitud JOIN insumos i ON d.id_insumo = i.id_insumo", conn)
                df_ent = pd.read_sql_query("SELECT h.fecha_hora as Fecha, i.nombre_articulo as Artículo, h.cantidad_agregada as Cantidad, h.tipo_movimiento as Movimiento FROM historial_entradas h JOIN insumos i ON h.id_insumo = i.id_insumo", conn)
            finally: conn.close()

            def conv_df(df):
                out = io.BytesIO(); df.to_excel(out, index=False); return out.getvalue()
                
            st.info("💡 **Sistema de Compras Inteligente:** El sistema detecta qué productos bajaron de su stock mínimo y calcula cuántos debes pedir para llenar la bodega (Máximo).")
            if not df_compras.empty: 
                st.download_button("🛒 Descargar Solicitud de Compras (Excel)", data=conv_df(df_compras), file_name=f"Pedido_{datetime.now().strftime('%Y%m%d')}.xlsx", type="primary")
            else:
                st.success("¡Tu bodega está perfecta! Ningún producto ha bajado de su nivel de alerta.")

            st.write("---")
            c1, c2, c3 = st.columns(3)
            if not df_inv.empty: c1.download_button("📥 Inventario Total", data=conv_df(df_inv), file_name="Inventario.xlsx")
            if not df_sol.empty: c2.download_button("📥 Salidas", data=conv_df(df_sol), file_name="Salidas.xlsx")
            if not df_ent.empty: c3.download_button("📥 Entradas", data=conv_df(df_ent), file_name="Entradas.xlsx")

        elif menu_admin == "Catálogo de Insumos":
            st.subheader("Catálogo de Insumos")
            tab_cat, tab_masiva = st.tabs(["📋 Gestión de Catálogo", "🚀 Carga Masiva (Excel)"])
            
            with tab_cat:
                with st.expander("➕ Agregar Nuevo Artículo al Catálogo", expanded=False):
                    c1, c2, c3 = st.columns(3)
                    n_nuevo = c1.text_input("Nombre del Producto", key="nn_art")
                    f_nuevo = c2.selectbox("Familia", ["PRODUCTOS", "SERVICIOS"], key="nf_art")
                    d_nuevo = c3.selectbox("Depto", ["EXAMENES", "SERVICIOS", "MEDICAMENTO", "ALIMENTOS", "VACUNAS", "ROPA", "ACCESORIOS"])
                    comp_nuevo = st.text_input("Componentes", key="n_comp")
                    
                    c4, c5 = st.columns(2)
                    s_min_nuevo = c4.number_input("Stock Mínimo (Alerta)", min_value=0, value=2)
                    s_max_nuevo = c5.number_input("Stock Máximo (Meta a llenar)", min_value=1, value=10)
                    
                    if st.button("Guardar Nuevo Producto", type="primary"):
                        if n_nuevo:
                            conn = get_connection()
                            try:
                                c = conn.cursor()
                                c.execute("INSERT INTO insumos (nombre_articulo, familia, departamento, componentes, existencia, stock_minimo, stock_maximo) VALUES (%s, %s, %s, %s, 0, %s, %s)", (n_nuevo.strip().upper(), f_nuevo, d_nuevo, comp_nuevo, s_min_nuevo, s_max_nuevo))
                                conn.commit()
                                st.success("Agregado exitosamente.")
                                time.sleep(1); st.rerun()
                            except IntegrityError: st.error("Ya existe un artículo con ese nombre.")
                            finally: conn.close()
                        else: st.error("El nombre es obligatorio.")

                st.write("### 📋 Directorio de Productos")
                conn = get_connection()
                try: df_cat = pd.read_sql_query("SELECT * FROM insumos ORDER BY nombre_articulo", conn)
                finally: conn.close()
                
                if not df_cat.empty:
                    df_display = df_cat[['nombre_articulo', 'componentes', 'stock_minimo', 'stock_maximo']].copy()
                    df_display['Imagen'] = df_cat['imagen_b64'].apply(lambda x: "✅" if pd.notna(x) and str(x).strip() != "" else "🚫")
                    df_display.columns = ['Producto', 'Componentes', 'Mínimo', 'Máximo', 'Imagen']
                    st.dataframe(df_display, use_container_width=True, hide_index=True)
                    
                    st.write("---")
                    st.write("### ✏️ Editar / Eliminar Artículo")
                    art_edit = st.selectbox("Selecciona un artículo de la tabla para modificarlo:", df_cat['nombre_articulo'].tolist())
                    datos_art = df_cat[df_cat['nombre_articulo'] == art_edit].iloc[0]
                    
                    # Manejo de estado para que la IA funcione sin borrar lo escrito
                    if "edit_comp_val" not in st.session_state or st.session_state.get("last_edit_art") != art_edit:
                        st.session_state["edit_comp_val"] = datos_art['componentes'] if pd.notna(datos_art['componentes']) else ""
                        st.session_state["last_edit_art"] = art_edit
                    
                    col_e1, col_e2, col_e3 = st.columns(3)
                    n_edit = col_e1.text_input("Nombre", value=datos_art['nombre_articulo'])
                    fam_index = ["PRODUCTOS", "SERVICIOS"].index(datos_art['familia']) if datos_art['familia'] in ["PRODUCTOS", "SERVICIOS"] else 0
                    f_edit = col_e2.selectbox("Familia", ["PRODUCTOS", "SERVICIOS"], index=fam_index)
                    dept_opts = ["EXAMENES", "SERVICIOS", "MEDICAMENTO", "ALIMENTOS", "VACUNAS", "ROPA", "ACCESORIOS"]
                    dept_index = dept_opts.index(datos_art['departamento']) if datos_art['departamento'] in dept_opts else 2
                    d_edit = col_e3.selectbox("Depto", dept_opts, index=dept_index)
                    
                    c_ia1, c_ia2 = st.columns([3, 1])
                    c_edit = c_ia1.text_input("Componentes", value=st.session_state["edit_comp_val"])
                    
                    st.write("") # Espaciador
                    if c_ia2.button("✨ Sugerir con IA"):
                        res = obtener_componentes_ia(n_edit, API_KEY_GLOBAL)
                        if "ERROR" not in res:
                            st.session_state["edit_comp_val"] = res
                            st.rerun()
                        else: st.error(res)
                    
                    col_e4, col_e5 = st.columns(2)
                    min_edit = col_e4.number_input("Stock Mín", min_value=0, value=int(datos_art['stock_minimo']))
                    max_edit = col_e5.number_input("Stock Máx", min_value=1, value=int(datos_art['stock_maximo']))
                    
                    img_upload = st.file_uploader("Subir / Actualizar Imagen", type=["jpg", "png", "jpeg"])
                    
                    col_btn_e1, col_btn_e2 = st.columns(2)
                    if col_btn_e1.button("✅ Guardar Cambios al Artículo", type="primary"):
                        if n_edit.strip() == "": st.error("El nombre no puede estar vacío.")
                        else:
                            conn = get_connection()
                            try:
                                c = conn.cursor()
                                if img_upload:
                                    img_b64 = base64.b64encode(img_upload.getvalue()).decode()
                                    c.execute("UPDATE insumos SET nombre_articulo=%s, familia=%s, departamento=%s, componentes=%s, stock_minimo=%s, stock_maximo=%s, imagen_b64=%s WHERE id_insumo=%s", 
                                              (n_edit.strip().upper(), f_edit, d_edit, c_edit, min_edit, max_edit, img_b64, int(datos_art['id_insumo'])))
                                else:
                                    c.execute("UPDATE insumos SET nombre_articulo=%s, familia=%s, departamento=%s, componentes=%s, stock_minimo=%s, stock_maximo=%s WHERE id_insumo=%s", 
                                              (n_edit.strip().upper(), f_edit, d_edit, c_edit, min_edit, max_edit, int(datos_art['id_insumo'])))
                                conn.commit()
                                st.success("Artículo actualizado.")
                                time.sleep(1); st.rerun()
                            except IntegrityError: st.error("Ese nombre ya está en uso.")
                            finally: conn.close()
                            
                    if col_btn_e2.button("🗑️ Eliminar Artículo"):
                        conn = get_connection()
                        try:
                            c = conn.cursor()
                            c.execute("DELETE FROM insumos WHERE id_insumo = %s", (int(datos_art['id_insumo']),))
                            conn.commit()
                            st.success("Artículo eliminado.")
                            time.sleep(1); st.rerun()
                        except IntegrityError: st.error("⚠️ No se puede eliminar: el artículo tiene historial.")
                        finally: conn.close()
                else:
                    st.info("No hay artículos en el catálogo.")
                    
            with tab_masiva:
                st.write("**Carga Masiva desde Excel**")
                st.info("El archivo Excel debe tener la columna (en mayúsculas): PRODUCTO. Opcionales: MINIMO, MAXIMO, FAMILIA, DEPARTAMENTO.")
                archivo_masivo = st.file_uploader("Sube tu catálogo completo", type=["xls", "xlsx"])
                
                if archivo_masivo:
                    df_masivo = pd.read_excel(archivo_masivo)
                    df_masivo.columns = [str(c).strip().upper() for c in df_masivo.columns]
                    
                    if 'PRODUCTO' in df_masivo.columns:
                        if st.button("🚀 Iniciar Carga de Catálogo", type="primary"):
                            conn = get_connection()
                            try:
                                c = conn.cursor()
                                count_ok = 0
                                count_errores = 0
                                for _, row in df_masivo.iterrows():
                                    p = str(row['PRODUCTO']).strip().upper()
                                    if pd.isna(p) or p == "NAN" or p == "":
                                        continue
                                        
                                    f = str(row.get('FAMILIA', 'PRODUCTOS')).strip().upper() if 'FAMILIA' in df_masivo.columns and pd.notna(row.get('FAMILIA')) else 'PRODUCTOS'
                                    d = str(row.get('DEPARTAMENTO', 'MEDICAMENTO')).strip().upper() if 'DEPARTAMENTO' in df_masivo.columns and pd.notna(row.get('DEPARTAMENTO')) else 'MEDICAMENTO'
                                    
                                    try: s_min = int(float(row.get('MINIMO', 0))) if 'MINIMO' in df_masivo.columns else 0
                                    except: s_min = 0
                                    
                                    try: s_max = int(float(row.get('MAXIMO', 0))) if 'MAXIMO' in df_masivo.columns else 0
                                    except: s_max = 0
                                    
                                    try:
                                        c.execute("""
                                            INSERT INTO insumos (nombre_articulo, familia, departamento, existencia, stock_minimo, stock_maximo) 
                                            VALUES (%s, %s, %s, 0, %s, %s)
                                            ON CONFLICT (nombre_articulo) 
                                            DO UPDATE SET stock_minimo = EXCLUDED.stock_minimo, stock_maximo = EXCLUDED.stock_maximo
                                        """, (p, f, d, s_min, s_max))
                                        count_ok += 1
                                    except Exception as e:
                                        count_errores += 1
                                
                                conn.commit()
                                st.success(f"✅ ¡Éxito! Se subieron o actualizaron {count_ok} artículos en la base de datos.")
                                if count_errores > 0: st.warning(f"Hubo {count_errores} filas que no se pudieron procesar.")
                            finally:
                                conn.close()
                    else:
                        st.error("❌ El archivo no tiene la columna 'PRODUCTO'. Revisa el encabezado de tu Excel.")
                        
        elif menu_admin == "Gestión de Personal":
            st.subheader("👥 Gestión de Personal")
            
            # 1. Agregar nuevo
            with st.form("form_agregar_personal", clear_on_submit=True):
                st.write("**Agregar Nuevo Colaborador**")
                n_pers = st.text_input("Nombre completo")
                if st.form_submit_button("Guardar Nombre") and n_pers:
                    conn = get_connection()
                    try:
                        c = conn.cursor()
                        c.execute("INSERT INTO personal (nombre) VALUES (%s)", (n_pers.strip().upper(),))
                        conn.commit()
                        st.success("Registrado.")
                        time.sleep(1)
                        st.rerun()
                    except IntegrityError: 
                        st.error("Ya existe en la base de datos.")
                    finally: 
                        conn.close()
            
            st.divider()
            
            # 2. Listar, Editar y Eliminar
            st.write("**Directorio de Personal**")
            conn = get_connection()
            try:
                df_personal = pd.read_sql_query("SELECT id_personal, nombre FROM personal ORDER BY nombre ASC", conn)
            finally:
                conn.close()
                
            if not df_personal.empty:
                # Mostramos la tabla actual
                st.dataframe(df_personal, use_container_width=True, hide_index=True)
                
                st.write("---")
                st.write("**Editar o Eliminar Personal**")
                persona_seleccionada = st.selectbox("Selecciona a la persona a modificar:", df_personal['nombre'].tolist())
                
                if persona_seleccionada:
                    id_persona = int(df_personal[df_personal['nombre'] == persona_seleccionada].iloc[0]['id_personal'])
                    
                    col_ed1, col_ed2 = st.columns([3, 1])
                    nuevo_nombre = col_ed1.text_input("Modificar nombre:", value=persona_seleccionada)
                    
                    col_btn1, col_btn2 = st.columns(2)
                    if col_btn1.button("Actualizar Nombre", type="primary"):
                        if nuevo_nombre.strip() == "":
                            st.error("El nombre no puede estar vacío.")
                        else:
                            conn = get_connection()
                            try:
                                c = conn.cursor()
                                c.execute("UPDATE personal SET nombre = %s WHERE id_personal = %s", (nuevo_nombre.strip().upper(), id_persona))
                                conn.commit()
                                st.success("Nombre actualizado.")
                                time.sleep(1)
                                st.rerun()
                            except IntegrityError:
                                st.error("Ese nombre ya existe.")
                            finally:
                                conn.close()
                                
                    if col_btn2.button("🗑️ Eliminar Persona"):
                        conn = get_connection()
                        try:
                            c = conn.cursor()
                            c.execute("DELETE FROM personal WHERE id_personal = %s", (id_persona,))
                            conn.commit()
                            st.success("Persona eliminada del sistema.")
                            time.sleep(1)
                            st.rerun()
                        except Exception as e:
                            st.error(f"Error al eliminar: {e}")
                        finally:
                            conn.close()
            else:
                st.info("No hay personal registrado aún.")
            
        elif menu_admin == "Configuración IA":
            n_llave = st.text_input("API Key:", value=API_KEY_GLOBAL, type="password")
            if st.button("Guardar"):
                conn = get_connection()
                try:
                    c = conn.cursor()
                    c.execute("INSERT INTO configuracion (parametro, valor) VALUES (%s, %s) ON CONFLICT (parametro) DO UPDATE SET valor = EXCLUDED.valor", ('gemini_api_key', n_llave.strip()))
                    conn.commit(); st.success("Guardado."); time.sleep(1); st.rerun()
                finally: conn.close()
