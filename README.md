# Bot personal de X/Twitter para Telegram — Render Webhook v1.0

Versión preparada el 22 de octubre de 2026. Ejecuta el bot en la nube sin mantener encendida tu computadora.

**Empieza por GUIA_RENDER.md.** Incluye todos los pasos desde BotFather y GitHub hasta las pruebas con otra persona.

## Lo que hace

- Cualquier persona que abra el bot puede descargar en un chat privado. No hay lista de usuarios autorizados.
- Recibe una publicación de X/Twitter por mensaje. Acepta enlaces móviles, /photo/1 y /video/1.
- Descarga sus videos, GIF convertidos a MP4 y fotos adjuntas. No recorre todo el hilo ni el perfil.
- Envía autor y texto de la publicación en negrita, junto al enlace original.
- Copia los archivos al canal privado del administrador, con nombre, @usuario si existe, ID numérico y horas de solicitud y descarga en Puerto Rico.
- Reutiliza el identificador del archivo en Telegram para la copia, evitando una segunda subida de los mismos bytes.
- Fotos como archivos originales por defecto. PHOTO_MODE=foto permite vistas de foto, con la compresión de Telegram.
- Divide videos grandes en partes menores de 49 MB, sin recomprimir cuando los fotogramas clave lo permiten.
- Envía textos largos en mensajes adicionales. Avisa si una publicación solo se pudo descargar parcialmente.

## Decisiones que resuelven el bloqueo anterior

OWNER_ID identifica a quien puede configurar el archivo. **No autoriza ni rechaza descargas.**

ALLOWED_USER_IDS y ACCESS_CHAT_ID no se utilizan. El bot funciona aunque un usuario no tenga @username.

En Render se inicia **server.py**, como un **Web Service con Docker y webhook**. Telegram envía un aviso HTTPS cuando alguien escribe; ese tráfico puede despertar la instancia gratuita.

## Archivos

| Archivo | Función |
| --- | --- |
| GUIA_RENDER.md | Instrucciones completas y solución de problemas |
| server.py | Servidor web, webhook autenticado y comprobación /health |
| app.py | Comandos, cola de descargas, envíos y copias de archivo |
| descarga.py | Descarga de multimedia, respaldo yt-dlp y división con FFmpeg |
| core.py | Enlaces, configuración, formato y cola SQLite de archivo |
| requirements.txt | Dependencias verificadas |
| Dockerfile | Python 3.12 y FFmpeg incluidos |
| render.yaml | Configuración opcional mediante Render Blueprint |
| configurar.py | Asistente opcional para terminal o máquina virtual |
| tests/test_bot.py | Pruebas sin token y sin publicar mensajes reales |

## Límites reales

Render Free se suspende después de 15 minutos sin tráfico y puede tardar aproximadamente un minuto en despertar. También impone cuotas de uso y transferencia; no es una promesa de disponibilidad continua.

Los archivos temporales, las solicitudes todavía en memoria y la cola SQLite de reintentos **no sobreviven de forma garantizada a suspensión, reinicio o nuevo despliegue en Render Free**. Las copias que ya llegaron al canal permanecen en Telegram. Para configuración permanente, usa Environment de Render.

X puede bloquear descargas públicas o exigir iniciar sesión. No se incluyen cookies ni acceso a publicaciones privadas. La ruta principal usa la API pública gratuita de FxTwitter/FxEmbed; el respaldo de videos usa yt-dlp. Un fallo de esos servicios puede impedir una descarga.

Límites del despliegue incluido: una descarga simultánea, dos solicitudes por persona, veinte en espera, hasta 250 MB por archivo de origen y diez minutos para el proceso de descarga/preparación. Las publicaciones con más de veinte adjuntos o videos que requieran más de ochenta partes se rechazan.

## Validación realizada

- 23 pruebas automáticas aprobadas: acceso de otros usuarios, cuentas sin @usuario, webhook y secreto, deduplicación de actualizaciones, límites de texto, cola de archivo y división de video.
- Comparación de fotogramas decodificados antes y después de dividir: contenido conservado sin recomprimir.
- Descarga real de una foto pública y una publicación pública con dos videos de 720 × 1280.
- No se realizó un despliegue en tu cuenta de Render ni un envío real mediante tu bot, porque no se proporcionaron sus credenciales.

Para ejecutar las pruebas en un entorno Python con las dependencias y FFmpeg:

    python -m unittest discover -s tests -v
