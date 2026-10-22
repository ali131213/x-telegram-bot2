# Guía desde cero — Bot de X/Twitter en Render gratis

Preparada el 22 de octubre de 2026. El bot corre en Render. Tu computadora se usa solo para descargar/subir los archivos y no debe permanecer encendida.

## 1. Qué se encontró en el bot anterior

El app.py anterior contiene una función is_authorized que solo permite descargar si el número de Telegram coincide con OWNER_ID, aparece en ALLOWED_USER_IDS o pertenece a ACCESS_CHAT_ID. Si ninguna condición se cumple, devuelve False y muestra “No tienes acceso todavía”.

Eso permite identificar el mecanismo que te rechazaba. Sin ver las variables que efectivamente tenía cargadas la instancia, no se puede afirmar si era un ID equivocado, un cambio sin desplegar o una instancia antigua.

La nueva versión elimina esa barrera para las descargas. OWNER_ID protege únicamente los comandos administrativos. Una cuenta sin @usuario funciona.

El bot anterior ya utilizaba webhook: la mejora no consiste solamente en cambiar polling por webhook. Aquí también se corrigen el acceso, la descarga de varios adjuntos, el texto en negrita y la separación entre entrega y copia al archivo.

## 2. Opciones de alojamiento evaluadas

| Servicio | ¿Puede funcionar sin tu computadora? | Condiciones del nivel gratuito | Evaluación para este bot |
| --- | --- | --- | --- |
| Render Free | Sí, mediante Web Service y webhook | Se suspende tras 15 min sin tráfico; el despertar tarda alrededor de un minuto. Cuotas mensuales y disco temporal. | Primera opción para probar: ya conoces Render y el uso será de pocas personas. |
| Koyeb Free | Sí, mediante servicio web y webhook | 512 MB RAM, 0.1 vCPU, 2 GB SSD; se suspende tras una hora sin tráfico y no admite volúmenes persistentes en la instancia gratuita. | Alternativa, pero también tiene pausas y muy poca CPU. |
| Oracle Cloud Always Free | Sí, mediante una máquina virtual | Recursos elegibles gratuitos, sujetos a disponibilidad; Oracle puede recuperar instancias consideradas inactivas. Requiere administrar un servidor. | Más control, pero el alta, la capacidad disponible y la configuración añaden dificultad. |
| Google Cloud Run | Sí | El Free Tier estándar tiene cuotas y requiere cuenta de facturación; el exceso puede generar cargos. Además, las tareas tras responder al webhook necesitan una arquitectura apropiada. | No es la ruta elegida para empezar con presupuesto cero. |

Recomendación: **probar Render Free con el paquete incluido**. Puede operar por sí solo, con demora al despertar. No se promete un servidor gratuito encendido permanentemente.

## 3. Antes de comenzar

Necesitas:

1. Una cuenta de Telegram.
2. Una cuenta de GitHub.
3. Tu cuenta de Render.
4. El ZIP descomprimido de esta versión.

No necesitas una API pagada de X, contratar un dominio, instalar Python en tu Mac ni dejar una terminal encendida.

Si Render te exige un método de pago por sus controles de cuenta, verifica lo que muestra antes de continuar: el código no elimina requisitos de registro del proveedor. Elige siempre el plan Free.

## 4. Evita que el servicio anterior interfiera

Si vas a reutilizar el mismo bot/token, suspende primero su servicio anterior de Render. No mantengas dos proyectos distintos configurando el webhook del mismo token: cada uno puede sustituir la dirección del otro.

Si prefieres empezar completamente limpio, crea un bot nuevo con BotFather y usa ese token. Puedes conservar el canal de archivo, añadiendo el bot nuevo como administrador.

No debes borrar tu canal ni sus videos. La guía no requiere borrar el proyecto anterior; suspenderlo permite conservarlo como referencia.

## 5. Crea el bot en Telegram

1. Abre el BotFather oficial: https://t.me/BotFather
2. Envía /newbot.
3. Elige el nombre visible.
4. Elige un nombre de usuario que termine en bot.
5. Copia el token que te entregue.

Trata el token como una contraseña. Se pega únicamente en Environment de Render. No lo publiques en GitHub, capturas de pantalla, mensajes de ayuda ni en una URL del navegador.

Si reutilizas el bot existente y conservas su token, omite la creación. Para sustituir un token expuesto, usa las opciones de revocación de BotFather y actualiza Render.

## 6. Crea el repositorio en GitHub

1. Ve a https://github.com/new
2. Nombre sugerido: x-telegram-bot.
3. Puedes marcarlo Private.
4. Crea el repositorio.
5. Usa “uploading an existing file” o “Add file → Upload files”.
6. Sube el contenido de la carpeta x_telegram_bot que sale al descomprimir el ZIP.
7. Confirma con “Commit changes”.

**El Dockerfile debe quedar en la raíz del repositorio**, al mismo nivel que server.py, app.py y requirements.txt. No subas el ZIP como un único archivo. No dejes todos los archivos dentro de una subcarpeta adicional por accidente.

Incluye los directorios tests si quieres conservar las pruebas. No crees ni subas un archivo .env con tu token. El .env.example contiene solo campos vacíos y es seguro.

## 7. Crea el servicio correcto en Render

1. Abre https://dashboard.render.com/
2. Selecciona “New” → “Web Service”.
3. Conecta GitHub si Render lo solicita.
4. Selecciona el repositorio que acabas de crear.
5. Nombre sugerido: x-telegram-bot.
6. Selecciona la rama donde subiste los archivos, normalmente main.
7. Runtime/Language: **Docker**.
8. Root Directory: vacío, si los archivos están en la raíz.
9. Dockerfile Path: ./Dockerfile.
10. Instance Type/Plan: **Free**.
11. Health Check Path: **/health**, si se muestra en Advanced.

No elijas Background Worker. El plan gratuito elegido es un Web Service.

No añadas un comando como “python app.py” o un servidor Flask de ejemplo. El Dockerfile ya inicia “python server.py”, que recibe el webhook y ejecuta las descargas.

Con Docker, Render construye la imagen e instala Python, FFmpeg y las dependencias. No tienes que instalar FFmpeg manualmente.

## 8. Añade las variables iniciales

En Environment del nuevo servicio:

| Clave | Valor inicial | Para qué se usa |
| --- | --- | --- |
| BOT_TOKEN | El token real de BotFather | Conectar con tu bot |
| TIMEZONE | America/Puerto_Rico | Horas de solicitud y descarga |
| PHOTO_MODE | original | Fotos como archivos sin recomprimir |

Deja OWNER_ID y ARCHIVE_CHAT_ID sin crear hasta obtener los IDs, si todavía no los sabes. **No escribas 0, @usuario, un enlace ni texto de ejemplo en esos campos.**

No añadas ALLOWED_USER_IDS ni ACCESS_CHAT_ID: esta versión no los utiliza.

RENDER_EXTERNAL_URL lo proporciona Render automáticamente. No tienes que inventarlo ni copiarlo desde otro proyecto.

WEBHOOK_SECRET es opcional: si falta, el código deriva un secreto del token. Si quieres definirlo, utiliza de 24 a 256 letras, números, guiones o guiones bajos.

Ahora pulsa “Deploy Web Service”.

## 9. Comprueba el primer despliegue

1. Espera a que Render indique “Live”.
2. En Logs debe aparecer “Webhook activo” y “Acceso abierto a todos los chats privados”.
3. Abre la dirección pública del servicio añadiendo /health al final.
4. Debe responder con ok: true y service: x-telegram-bot.

Un /health correcto confirma que el proceso está preparado; no demuestra por sí solo que X haya permitido descargar un video.

En Telegram abre tu bot:

    /version

Debe responder:

    X Archive Bot · Render Webhook v1.0
    Descargas abiertas: sin lista de usuarios autorizados.

Después:

    /start
    /id

El bot debe responder aunque no hayas configurado OWNER_ID. Anota el número personal que devuelve /id.

Si todavía dice “No tienes acceso todavía”, el código anterior sigue atendiendo ese bot: revisa qué servicio está activo, el repositorio/rama y el token. Esa frase de rechazo no existe en el flujo de descarga de la nueva versión.

## 10. Identifícate como administrador

1. En Render → Environment añade OWNER_ID.
2. Pega el número personal devuelto por /id, sin @ y sin espacios.
3. Guarda y aplica los cambios para que Render reinicie/despliegue el servicio.
4. Espera otra vez a que esté Live.

Ahora puedes usar /canales y /archivo.

**OWNER_ID no restringe a tus invitados.** Los demás siguen pudiendo descargar.

## 11. Crea el canal privado de archivo

En Telegram:

1. Crea un canal, por ejemplo “Archivo X”.
2. Elige que sea privado.
3. Añade tu bot como administrador.
4. Permite que publique mensajes.
5. No hace falta darle permisos para añadir administradores ni invitar a tus usuarios.
6. Publica un mensaje nuevo dentro del canal, por ejemplo “Configurando archivo”.

El bot registra los canales donde recibe publicaciones o avisos de membresía. No responde a esas publicaciones con descargas, por lo que sus propias copias no crean un bucle.

## 12. Obtén y vincula el ID del canal

En tu chat privado con el bot:

    /canales

Debe mostrar el nombre y un ID negativo, normalmente empezando por -100.

Si no aparece, deja que el bot esté activo y publica otro mensaje nuevo en el canal. También puedes reenviar desde el canal un mensaje al chat privado del bot; si Telegram incluye el origen del canal, el bot reconocerá el ID.

Cuando lo tengas:

    /archivo -1001234567890

Sustituye el ejemplo por el ID real. El bot verifica que se trate de un canal, revisa el permiso de publicación y manda un mensaje de prueba al archivo.

**Paso indispensable en Render:** añade también ese número a Environment como ARCHIVE_CHAT_ID y guarda/aplica el cambio. La vinculación mediante /archivo por sí sola usa un archivo local temporal, que Render Free puede perder al suspenderse o reiniciarse.

Tras el despliegue, usa:

    /estado

Como dueño verás el canal configurado y el número de copias de archivo pendientes.

## 13. Prueba la descarga completa

1. Envía un enlace público de X que contenga un video corto.
2. El bot debe confirmar la recepción y preparar la descarga.
3. Debes recibir el archivo con autor, texto de la publicación en negrita y enlace original.
4. Abre tu canal: debe haber una copia con esa misma información, más tus datos y las horas.
5. Prueba también una publicación con varias fotos o varios videos.
6. Pide a una segunda persona que abra el bot, pulse Start y envíe otro enlace.
7. Esa persona debe recibir sus archivos sin que añadas su ID a ninguna variable.

Ejemplo conceptual de la información del canal:

    Nombre del autor (@cuenta_de_x)
    Texto de la publicación
    Video 1/2
    https://x.com/i/status/ID

    Solicitante: Nombre (@usuario_de_telegram)
    ID: 123456789
    Solicitud: 2026-10-22 14:00:00
    Descarga: 2026-10-22 14:00:12
    Zona: America/Puerto_Rico

El autor y el texto original se envían en negrita. Si alguien no tiene @usuario, se registra su nombre y su ID numérico.

## 14. Cómo se comporta el bot con tus invitados

Comparte únicamente el enlace de Telegram de tu bot. No necesitan acceso al canal de archivo, suscribirse, conocer el token ni estar añadidos manualmente.

El primer mensaje informa de que sus descargas y sus datos de identificación se copian al archivo privado del administrador.

Tener el enlace permite usar el bot. Si otra persona recibe ese enlace, también podrá usarlo: es el acceso abierto que has solicitado.

Se admiten chats privados. El bot no ejecuta descargas en grupos ni en el propio canal.

## 15. Calidad, fotos, GIF y videos grandes

- La ruta principal obtiene la mejor variante MP4 que el servicio devuelve y que se pueda descargar dentro de los límites. No aumenta artificialmente la resolución.
- Las fotos se solicitan en tamaño original y se envían como documentos para evitar la compresión de “enviar foto”.
- Si prefieres verlas como fotos en la conversación/galería, cambia PHOTO_MODE a foto y aplica los cambios en Render. Telegram puede comprimirlas; si no admite sus dimensiones, se mandan como documento.
- Los GIF de X se entregan como archivos MP4 reproducibles.
- Si un video supera 49.000.000 bytes, FFmpeg intenta dividirlo en fotogramas clave sin recomprimir. Cada parte lleva su número.
- Hay videos cuyos fotogramas clave no permiten obtener partes dentro del límite. En ese caso se informa del fallo; no se promete conservar siempre un único archivo completo.
- Los textos largos se distribuyen en mensajes adicionales.
- Si falta algún adjunto o se usa el respaldo yt-dlp sin poder verificar las fotos, el bot avisa. No informa de una descarga completa cuando sabe que fue parcial.

El límite actual de la API estándar de bots de Telegram es 50 MB para subir videos/documentos. Se usa un margen de 49 MB. Un límite mayor en la aplicación personal de Telegram no aumenta automáticamente el límite de un bot.

## 16. Qué esperar cuando Render se duerme

Render Free suspende el servicio después de 15 minutos sin tráfico entrante. Telegram envía las solicitudes nuevas a /webhook, y esa petición puede iniciar el servicio de nuevo.

Render indica un tiempo de despertar de alrededor de un minuto. Telegram reintenta entregas fallidas, con un número finito de intentos. Si no aparece respuesta tras esperar uno o dos minutos, reenvía /estado una vez y consulta Events/Logs.

No se incluye un sistema de pings para simular actividad. El diseño utiliza las solicitudes reales de Telegram.

No hace falta que tu computadora esté encendida. La primera respuesta después de un período de inactividad puede tardar.

La instancia tiene cuotas de transferencia y 750 horas gratuitas mensuales compartidas por el workspace de Render. Suspender el servicio viejo evita mantener dos servicios consumiendo esa bolsa.

## 17. Copias de archivo y sus límites

La entrega al usuario no depende de que el canal acepte inmediatamente una copia. Después de subir al usuario, se guarda el identificador del archivo y se intenta copiar al canal, sin volver a subir los mismos bytes.

Si el canal falla, se reintenta mientras el proceso sigue activo. /estado muestra el número pendiente al administrador. Revisa los permisos del canal si ese número crece.

**Render Free no ofrece disco persistente para este servicio.** Al suspenderse, reiniciarse o desplegarse de nuevo puede perder:

- Solicitudes que estaban en memoria.
- Archivos de una descarga en curso.
- Copias pendientes en SQLite que todavía no llegaron al canal.
- Canales detectados y cambios hechos solo en un .env local.

Las copias ya publicadas en Telegram siguen en tu canal. El token y los IDs guardados en Environment de Render sobreviven a los reinicios.

Si el servicio se reinicia durante una descarga, vuelve a enviar el enlace. Esta versión no garantiza procesamiento sin pérdidas durante reinicios; eso requeriría una cola y almacenamiento externos persistentes o un servidor con disco persistente.

## 18. Solución de problemas

| Síntoma | Comprobación |
| --- | --- |
| Sigue diciendo “No tienes acceso todavía” | Sigue activo el código viejo o estás probando otro bot. Comprueba /version, token, repositorio y servicio anterior. |
| /start no responde inmediatamente | Espera el despertar de Render; revisa Live, /health y Logs. |
| /canales o /archivo dice que es solo del administrador | OWNER_ID debe ser el número personal devuelto por /id. Guarda y aplica cambios. Esto no impide descargar. |
| No aparece el canal | Bot como administrador, permiso para publicar y una publicación nueva mientras corre el servicio. |
| ID del canal incorrecto | Usa el número negativo del canal; no su enlace de invitación, nombre, @alias ni tu ID personal. |
| /archivo funciona y luego desaparece la vinculación | Falta guardar ARCHIVE_CHAT_ID en Environment de Render. |
| “InvalidToken” en Logs | El token no corresponde, se copió mal o fue revocado. Actualízalo en Environment. |
| /health devuelve 404 o una página de otro proyecto | Revisa la URL y que el servicio ejecute el Dockerfile de esta versión. |
| “Falta RENDER_EXTERNAL_URL” | Debe ser un Render Web Service. Para otro proveedor define PUBLIC_URL con su dominio HTTPS. |
| Responde pero no descarga un enlace concreto | Comprueba que tenga multimedia pública. X puede exigir login o bloquear el acceso; prueba otro enlace conocido. |
| El usuario recibe archivos, pero el canal no | Revisa ARCHIVE_CHAT_ID, permisos y las copias pendientes en /estado. |
| Solo falla una publicación larga | El proceso tiene diez minutos de preparación y 250 MB máximos por archivo de origen. Prueba uno más pequeño. |
| El bot se comporta de dos maneras distintas | Dos instancias están usando el mismo token o cambiando su webhook. Suspende la anterior. |
| Render suspende el servicio por cuotas | Revisa el consumo del workspace. Un video consume transferencia; el nivel gratis no es ilimitado. |

Para investigar un problema basta con /version, /estado y el error de Logs ocultando cualquier secreto. No necesitas enviar el token a otra persona.

## 19. Actualizaciones

Si X cambia y empiezan a fallar enlaces que antes funcionaban:

1. Revisa si existe una versión nueva de yt-dlp en su proyecto oficial.
2. Edita la línea de yt-dlp en requirements.txt con una versión disponible y actual.
3. Confirma el cambio en GitHub.
4. Espera el nuevo despliegue de Render y vuelve a probar.

La API pública de FxTwitter/FxEmbed también puede tener interrupciones o cambios. No existe una garantía de que un descargador gratuito pueda obtener todas las publicaciones de X.

## 20. Ruta alternativa con render.yaml

El archivo render.yaml permite usar “New → Blueprint” en lugar del asistente manual de Web Service. Crea un servicio Docker en Free y pide las variables privadas.

Si todavía no tienes los IDs, la ruta manual de esta guía es más sencilla porque permite desplegar primero solo con el token y obtenerlos con /id y /canales.

No necesitas crear una base de datos de Render para esta versión. La base Postgres gratuita de Render expira tras 30 días y no se añadió como una dependencia permanente.

## Fuentes oficiales consultadas

- Render, plan gratuito, suspensión, cuotas y archivos temporales: https://render.com/docs/free
- Render, Web Services: https://render.com/docs/web-services
- Telegram, creación mediante BotFather: https://core.telegram.org/bots/tutorial
- Telegram, setWebhook, autenticación, reintentos y límites de envío: https://core.telegram.org/bots/api
- python-telegram-bot: https://docs.python-telegram-bot.org/en/stable/
- yt-dlp: https://github.com/yt-dlp/yt-dlp
- FxEmbed/FxTwitter, API pública: https://docs.fxembed.com/
- Koyeb, instancias gratuitas: https://www.koyeb.com/docs/reference/instances
- Oracle, recursos Always Free y recuperación de instancias inactivas: https://docs.oracle.com/en-us/iaas/Content/FreeTier/freetier_topic-Always_Free_Resources.htm
- Google Cloud, Free Tier estándar: https://docs.cloud.google.com/free/docs/free-cloud-features

Los nombres de algunos controles de Render/GitHub pueden variar. Usa las equivalencias indicadas: Web Service, Docker, Free, Environment y /health.
