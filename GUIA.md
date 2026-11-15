# Guía de instalación: buscador y notificador de licitaciones (Fase 1)

**Qué hace:** dos veces al día (7:07 a. m. y 6:07 p. m., hora Perú) revisa en el SEACE todas las entidades del país. Guarda las **obras, supervisiones y consultorías de obra** en tu buscador web y te envía por correo las nuevas que pasan tus filtros. Las de tus regiones prioritarias aparecen primero.

**Costo:** S/ 0. Usa GitHub (gratis) y tu correo.
**Tiempo de instalación:** 30–40 minutos, una sola vez.

---

## Paso 1. Crear tu cuenta de GitHub
1. Entra a **github.com** → *Sign up*.
2. Usa tu correo de siempre y elige un usuario corto (ej. `ferpisco`). Ese usuario va a aparecer en la dirección de tu buscador.

## Paso 2. Crear el repositorio (la "carpeta" del sistema)
1. Arriba a la derecha: **+** → **New repository**.
2. Nombre: `buscador-licitaciones`
3. Marca **Public**. Es obligatorio para que el buscador web sea gratis. Solo se publican licitaciones, que ya son información pública. Tus claves de correo NO se publican (van en el Paso 5).
4. Clic en **Create repository**.

## Paso 3. Subir los archivos
1. Descomprime el ZIP en tu computadora.
2. En la página del repositorio: **Add file → Upload files** (o el enlace "uploading an existing file").
3. Arrastra **todo el contenido** de la carpeta descomprimida: las carpetas `buscador`, `docs`, `pruebas`, `.github` y los archivos sueltos.
4. Abajo, clic en **Commit changes**.

> ⚠️ **Revisa que se haya subido la carpeta `.github`.** En Mac (y a veces en Windows) esa carpeta viene oculta. Si no aparece en el repositorio:
> **Add file → Create new file** → en el nombre escribe exactamente `.github/workflows/buscador.yml` → pega el contenido del archivo `COPIA_workflow_buscador.yml` (está en la carpeta principal; ábrelo con el Bloc de notas) → **Commit changes**.

## Paso 4. Preparar el correo (elige UNA opción)

### Opción A: Gmail con "contraseña de aplicación"
Sirve con un Gmail nuevo del bot o con tu Gmail actual.
1. Entra a **myaccount.google.com** con la cuenta que va a ENVIAR.
2. **Seguridad** → activa la **Verificación en 2 pasos**.
3. En el buscador de arriba de esa página escribe **"Contraseñas de aplicaciones"**.
4. Nombre: `buscador` → **Crear** → copia la clave de 16 letras.

### Opción B: Brevo (si no quieres darle acceso a tu Gmail)
1. Regístrate gratis en **brevo.com** con tu correo de siempre y confírmalo.
2. Menú de tu cuenta → **SMTP & API** → pestaña **API Keys** → **Generate a new API key** → copia la clave.
3. Esa clave solo sirve para enviar correos. No puede leer tu bandeja.

## Paso 5. Guardar las claves en GitHub (Secrets)
En tu repositorio: **Settings → Secrets and variables → Actions → New repository secret**. Crea cada uno por separado:

| Nombre (exacto) | Valor | ¿Cuándo? |
|---|---|---|
| `CORREO_DESTINO` | El correo donde quieres recibir las alertas. Pueden ser varios separados por coma | Siempre |
| `GMAIL_USUARIO` | El Gmail que envía (ej. `algo@gmail.com`) | Opción A |
| `GMAIL_APP_PASSWORD` | La clave de 16 letras | Opción A |
| `BREVO_API_KEY` | La clave de Brevo | Opción B |
| `BREVO_REMITENTE` | El correo con el que te registraste en Brevo | Opción B |

## Paso 6. Dar permiso para guardar resultados
**Settings → Actions → General** → al final, en *Workflow permissions*, marca **Read and write permissions** → **Save**.

## Paso 7. Activar tu buscador web
**Settings → Pages** → en *Source* elige **Deploy from a branch** → Branch: **main**, carpeta **/docs** → **Save**.
Unos minutos después tu buscador estará en:
**`https://TU-USUARIO.github.io/buscador-licitaciones/`**
Guárdalo en la pantalla de inicio de tu celular. En el navegador, "Agregar a pantalla de inicio" y queda como una app.

## Paso 8. Primera ejecución (prueba)
1. Pestaña **Actions** → si aparece un botón verde para habilitar los workflows, acéptalo.
2. A la izquierda: **Buscador de licitaciones** → **Run workflow** → en "diagnóstico" escribe `si` → **Run workflow**.
3. Espera entre 5 y 30 minutos. Al terminar sale ✅ verde.
4. Entra a la ejecución → **buscar** → abre el paso **"Buscar licitaciones y enviar correo"** y copia estas líneas:
   - `Páginas leídas: … | fecha más antigua alcanzada: …`
   - `Procesos nuevos de obra/supervisión/consultoría: …`
   - Las líneas que empiezan con `DIAGNÓSTICO`
5. **Envíame esas líneas** y el archivo `muestra_api.json` que se creó en el repositorio. Con eso ajusto la clasificación al formato real de la API (región, tipo, montos) y quedamos afinados.

La primera vez revisa los **últimos 3 días**. Desde ahí funciona solo, dos veces al día, y solo te escribe cuando hay algo nuevo.

---

## Cambiar tus filtros (sin programar)
Abre `buscador/config.json` en GitHub → ícono del **lápiz** → edita → **Commit changes**.

- `regiones_prioritarias`: las que salen arriba en el correo.
- `montos_para_correo`: mínimo y máximo por tipo.
- `palabras_excluidas`: lo que no quieres que te llegue.
- `solo_estas_especialidades`: déjalo `[]` para recibir todas. Para limitar, copia nombres exactos de la lista `especialidades`, por ejemplo `["Agua y saneamiento", "Movimiento de tierras y obras civiles"]`.
- `especialidades`: puedes agregar palabras clave a cada especialidad.

El buscador web siempre guarda **todo** (obras, supervisión y consultoría de obra de todo el país, sin límite de monto). Los filtros solo deciden qué te llega por correo.

## Problemas comunes

| Síntoma | Solución |
|---|---|
| ❌ rojo en Actions con "La API del OECE no respondió" | Suele ser caída temporal del portal. Si falla 2 veces seguidas te llega un correo de aviso. Si persiste, mándame el error |
| ❌ con "Brevo rechazó" o error de login de Gmail | Revisa los nombres y valores de los *Secrets* (Paso 5). En Gmail, la clave de 16 letras va sin espacios |
| No llegan correos | Revisa **Spam** y marca "No es spam". También puede ser que no haya novedades que pasen tus filtros |
| `AVISO: se llegó al máximo de páginas` | Mándame el log y ajusto los límites |
| La página web dice "Sin datos" | Espera a que termine la primera ejecución y a que GitHub Pages publique (unos 5 min) |
| GitHub avisa que desactivó el workflow por inactividad | Pestaña Actions → **Enable workflow** |

## Lo que viene
- **Fase 2:** Obras por Impuestos (ProInversión, gob.pe, El Peruano) y comunicados de ANIN.
- **Fase 3:** Petroperú y vigilancia de anuncios de mineras.
