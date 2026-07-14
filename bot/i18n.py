"""
Interface strings — English (default) and Russian.

t(lang, key, **fmt) renders a string in the user's interface language, falling
back to English for anything missing. Admin/system alerts deliberately stay
inline English in the handlers (operator-facing).

The test suite enforces that every key exists in BOTH languages and that the
{placeholder} sets match — translations can't silently drift.
"""
import logging

logger = logging.getLogger(__name__)

DEFAULT_LANG = "en"
SUPPORTED = ("en", "ru")

STRINGS: dict[str, dict[str, str]] = {
    # ── guide (telegra.ph) ────────────────────────────────────────────────
    "guide_url": {
        "en": "https://telegra.ph/NewsStream-Bot--User-Guide-07-14",
        "ru": "https://telegra.ph/NewsStream--rukovodstvo-polzovatelya-07-14",
    },

    # ── /start (short welcome; the full command table lives in /help) ─────
    "start_user": {
        "en": "🗞️ <b>NewsStream</b> — your personal news researcher.\n\n"
              "Tell me in your own words what you want to stay on top of. I'll "
              "find the best sites covering it, watch them around the clock, and "
              "send you only the stories that match.\n\n"
              "📖 <a href=\"{guide}\">How it works — 2-minute guide</a>\n"
              "⌨️ /help lists every command.",
        "ru": "🗞️ <b>NewsStream</b> — ваш личный новостной ассистент.\n\n"
              "Расскажите своими словами, за чем хотите следить. Я найду лучшие "
              "сайты по теме, буду наблюдать за ними круглосуточно и присылать "
              "только те новости, которые вам нужны.\n\n"
              "📖 <a href=\"{guide}\">Как это работает — гид за 2 минуты</a>\n"
              "⌨️ /help — список всех команд.",
    },
    "help_user": {
        "en": """\
# ⌨️ Commands

Everything here is also available as buttons — just send `/menu`.

| Command | Description |
|---------|-------------|
| `/newstream` | Set up a news stream (just tell me what you want) |
| `/menu` | The button menu |
| `/streams` | List your streams |
| `/sources 3` | What stream 3 is watching |
| `/addsource 3 site.com` | Add a site you like to stream 3 |
| `/deletesource 12` | Remove source 12 from your stream |
| `/research 3` | Redo source research for stream 3 |
| `/latest` | Your latest collected articles |
| `/postsize 3` | Post length: standard / compact |
| `/language` | Bot language · `/language 3` — post language |
| `/pausestream 3` | Pause a stream |
| `/resumestream 3` | Resume a paused stream |
| `/deletestream 3` | Delete a stream |
| `/quiet 3 23-8` | No posts between those hours (`off` to clear) |

📖 [Full guide]({guide})\
""",
        "ru": """\
# ⌨️ Команды

Всё это есть и кнопками — просто отправьте `/menu`.

| Команда | Описание |
|---------|----------|
| `/newstream` | Создать новостной поток (просто расскажите, что нужно) |
| `/menu` | Меню с кнопками |
| `/streams` | Список ваших потоков |
| `/sources 3` | За чем следит поток 3 |
| `/addsource 3 site.com` | Добавить свой сайт в поток 3 |
| `/deletesource 12` | Убрать источник 12 из потока |
| `/research 3` | Заново подобрать источники для потока 3 |
| `/latest` | Последние собранные статьи |
| `/postsize 3` | Длина постов: обычная / короткая |
| `/language` | Язык бота · `/language 3` — язык постов |
| `/pausestream 3` | Приостановить поток |
| `/resumestream 3` | Возобновить поток |
| `/deletestream 3` | Удалить поток |
| `/quiet 3 23-8` | Тихие часы — без постов в это время (`off` — отключить) |

📖 [Полный гид]({guide})\
""",
    },

    # ── menu screens ──────────────────────────────────────────────────────
    "menu_main": {
        "en": "🗞️ <b>NewsStream</b>\n\nWhat would you like to do?",
        "ru": "🗞️ <b>NewsStream</b>\n\nЧто сделать?",
    },
    "btn_menu_newstream": {"en": "➕ New stream", "ru": "➕ Новый поток"},
    "btn_menu_streams": {"en": "📋 My streams", "ru": "📋 Мои потоки"},
    "btn_menu_latest": {"en": "📰 Latest articles", "ru": "📰 Последние статьи"},
    "btn_menu_language": {"en": "🌐 Language", "ru": "🌐 Язык"},
    "btn_menu_guide": {"en": "📖 Guide", "ru": "📖 Гид"},
    "btn_back": {"en": "⬅️ Back", "ru": "⬅️ Назад"},
    "menu_streams_title": {
        "en": "📋 <b>Your streams</b>\n\nPick one to manage it:",
        "ru": "📋 <b>Ваши потоки</b>\n\nВыберите, чтобы управлять:",
    },
    "menu_streams_empty": {
        "en": "📭 You have no streams yet — create your first and tell me what "
              "news you want.",
        "ru": "📭 Потоков пока нет — создайте первый и расскажите, какие новости "
              "вам нужны.",
    },
    "scr_stream": {
        "en": "<b>{name}</b>\n"
              "Status: {status}\n"
              "Sources: {n_sources}\n"
              "Posts: {length}, in {language}\n"
              "Quiet hours: {quiet}",
        "ru": "<b>{name}</b>\n"
              "Статус: {status}\n"
              "Источников: {n_sources}\n"
              "Посты: {length}, язык — {language}\n"
              "Тихие часы: {quiet}",
    },
    "word_standard": {"en": "standard", "ru": "обычные"},
    "word_compact": {"en": "compact", "ru": "короткие"},
    "word_off": {"en": "off", "ru": "выкл"},
    "btn_sources": {"en": "📰 Sources", "ru": "📰 Источники"},
    "btn_add_source": {"en": "➕ Add source", "ru": "➕ Добавить источник"},
    "btn_pause": {"en": "⏸ Pause", "ru": "⏸ Пауза"},
    "btn_resume": {"en": "▶️ Resume", "ru": "▶️ Возобновить"},
    "btn_postlen": {"en": "📏 Post length", "ru": "📏 Длина постов"},
    "btn_postlang": {"en": "🌐 Post language", "ru": "🌐 Язык постов"},
    "btn_quiet": {"en": "🔕 Quiet hours", "ru": "🔕 Тихие часы"},
    "btn_research": {"en": "🔄 Redo research", "ru": "🔄 Подобрать заново"},
    "btn_delete_stream": {"en": "🗑 Delete stream", "ru": "🗑 Удалить поток"},
    "menu_sources_title": {
        "en": "📰 <b>Sources of “{name}”</b>\n\nTap 🗑 to remove one:",
        "ru": "📰 <b>Источники потока «{name}»</b>\n\nНажмите 🗑, чтобы убрать:",
    },
    "menu_sources_empty": {
        "en": "This stream has no sources yet — add a site you like.",
        "ru": "У потока пока нет источников — добавьте сайт, который вам нравится.",
    },
    "menu_quiet_title": {
        "en": "🔕 <b>Quiet hours for “{name}”</b>\n\nNo posts inside the window; "
              "anything held arrives after it ends. Pick a window:",
        "ru": "🔕 <b>Тихие часы для «{name}»</b>\n\nВ это время посты не приходят; "
              "всё накопившееся придёт после. Выберите окно:",
    },
    "btn_quiet_off": {"en": "🔔 Off (24/7)", "ru": "🔔 Выкл (круглосуточно)"},
    "menu_plen_title": {
        "en": "📏 <b>Post length for “{name}”</b>",
        "ru": "📏 <b>Длина постов для «{name}»</b>",
    },
    "menu_slang_title": {
        "en": "🌐 <b>Post language for “{name}”</b>",
        "ru": "🌐 <b>Язык постов для «{name}»</b>",
    },
    "addsrc_prompt": {
        "en": "➕ Send me the site's address — just the site, like "
              "<code>techcrunch.com</code>. I'll find its news page myself.",
        "ru": "➕ Пришлите адрес сайта — просто сайт, например "
              "<code>techcrunch.com</code>. Страницу новостей я найду сам.",
    },
    "addsrc_not_a_url": {
        "en": "That doesn't look like a site address. Send something like "
              "<code>techcrunch.com</code> — or tap Back.",
        "ru": "Это не похоже на адрес сайта. Пришлите что-то вроде "
              "<code>techcrunch.com</code> — или нажмите «Назад».",
    },

    # ── interview ─────────────────────────────────────────────────────────
    "interview_opener": {
        "en": "What would you like me to keep you in the loop on?\n\n"
              "Tell me about the news you're after — a topic, a beat, a question "
              "you're trying to stay ahead of. As specific or as loose as you "
              "like, in your own words.",
        "ru": "О чём держать вас в курсе?\n\n"
              "Расскажите, какие новости вам нужны — тема, сфера, вопрос, за "
              "которым хотите следить. Можно очень конкретно, можно в общих "
              "чертах — своими словами.",
    },
    "cancelled": {
        "en": "❌ Stream creation cancelled.",
        "ru": "❌ Создание потока отменено.",
    },

    # ── limits ────────────────────────────────────────────────────────────
    "limit_streams": {
        "en": "You already have {max} streams — that's the limit for now. "
              "`/deletestream <id>` frees a slot.",
        "ru": "У вас уже {max} потоков — это текущий лимит. "
              "`/deletestream <id>` освободит место.",
    },
    "limit_research": {
        "en": "You've used your {max} research runs for today — try again tomorrow.",
        "ru": "Вы использовали все {max} подбора источников на сегодня — "
              "попробуйте завтра.",
    },
    "limit_sources": {
        "en": "This stream already follows {max} sources — that's the cap. "
              "Drop one with `/deletesource <id>` first.",
        "ru": "Этот поток уже следит за {max} источниками — это максимум. "
              "Сначала уберите один: `/deletesource <id>`.",
    },

    # ── research kickoff / results ────────────────────────────────────────
    "research_kickoff": {
        "en": "🔎 On it — I'm reading through the best sources on this now. "
              "This usually takes a minute or two.{lang_note}\n\nWhile I work: "
              "how long should each post be? (Standard by default — change "
              "anytime with /postsize.)",
        "ru": "🔎 Принято — уже изучаю лучшие источники по этой теме. Обычно "
              "это занимает минуту-две.{lang_note}\n\nПока я работаю: какой "
              "длины делать посты? (По умолчанию обычная — поменять можно в "
              "любой момент через /postsize.)",
    },
    "research_kickoff_lang_note": {
        "en": " Posts for this stream will be in Russian — `/language {stream_id}` "
              "to change.",
        "ru": " Посты этого потока будут на русском — `/language {stream_id}`, "
              "если нужно поменять.",
    },
    "res_header": {
        "en": "# ✅ Found {n} sources for you\n\nHere's what I'll be watching "
              "for **{name}**:\n",
        "ru": "# ✅ Нашёл для вас {n} источников\n\nВот за чем я буду следить "
              "для потока **{name}**:\n",
    },
    "res_table_header": {
        "en": "| # | Source | Match | Articles | Status |\n"
              "|---|--------|-------|----------|--------|",
        "ru": "| # | Источник | Совпадение | Статьи | Статус |\n"
              "|---|----------|------------|--------|--------|",
    },
    "res_live": {
        "en": "\n{n} source(s) are live and already listing articles — I'll "
              "start pulling relevant stories shortly.",
        "ru": "\n{n} источник(ов) уже работают и публикуют статьи — скоро "
              "начну присылать подходящие новости.",
    },
    "res_pending": {
        "en": "\n⏳ {n} showed no articles on the first look — I'll keep an eye "
              "on them and they'll switch on as soon as something appears.",
        "ru": "\n⏳ {n} при первой проверке не показали статей — я буду за ними "
              "присматривать, и они включатся, как только что-то появится.",
    },
    "res_blocked": {
        "en": "\n🔄 {n} were busy when I checked — I'll keep retrying them "
              "automatically and they'll switch on once reachable.",
        "ru": "\n🔄 {n} были недоступны при проверке — я буду автоматически "
              "пробовать снова, и они включатся, когда ответят.",
    },
    "res_all_aggregators": {
        "en": "\n⚠️ Everything live right now is an aggregator feed — I'd "
              "suggest `/addsource` with a publication you trust for "
              "first-party coverage.",
        "ru": "\n⚠️ Пока все работающие источники — агрегаторы. Советую "
              "добавить через `/addsource` издание, которому вы доверяете.",
    },
    "res_finetune": {
        "en": "\nWant to fine-tune? Open `/menu` → My streams → this stream: "
              "you can remove any source or add a site you like with a couple "
              "of taps.",
        "ru": "\nХотите настроить точнее? Откройте `/menu` → Мои потоки → этот "
              "поток: там пара нажатий, чтобы убрать источник или добавить "
              "свой сайт.",
    },
    "res_none": {
        "en": "I dug through a lot of sites but couldn't find sources solid "
              "enough to trust for this one yet — often that means the topic "
              "is very narrow, or worth phrasing a little differently.\n\n"
              "A couple of options:\n"
              "• `/research {stream_id}` — I'll take another pass at it.\n"
              "• Add a site you already like: `/menu` → My streams → this "
              "stream → Add source.",
        "ru": "Я просмотрел много сайтов, но пока не нашёл достаточно "
              "надёжных источников — обычно это значит, что тема очень узкая "
              "или её стоит сформулировать чуть иначе.\n\nВарианты:\n"
              "• `/research {stream_id}` — попробую ещё раз.\n"
              "• Добавьте сайт, который вам нравится: `/menu` → Мои потоки → "
              "этот поток → Добавить источник.",
    },
    "research_error": {
        "en": "❌ Research error: {e}",
        "ru": "❌ Ошибка при подборе источников: {e}",
    },
    "research_usage": {
        "en": "Add the stream number — e.g. `/research 3`. `/streams` shows "
              "the numbers.",
        "ru": "Добавьте номер потока — например, `/research 3`. Номера — в "
              "`/streams`.",
    },
    "research_rerun": {
        "en": "🔬 Re-running research for stream `{stream_id}`...",
        "ru": "🔬 Заново подбираю источники для потока `{stream_id}`...",
    },
    "research_started": {
        "en": "Research started in background. I'll update you with results.",
        "ru": "Подбор запущен в фоне — пришлю результаты, когда закончу.",
    },

    # ── shared errors ─────────────────────────────────────────────────────
    "invalid_stream_id": {
        "en": "I don't recognise that stream number — `/streams` shows yours.",
        "ru": "Не узнаю этот номер потока — `/streams` покажет ваши.",
    },
    "not_your_stream": {
        "en": "❌ That stream isn't yours.",
        "ru": "❌ Это не ваш поток.",
    },
    "stream_not_found": {
        "en": "❌ I can't find stream {stream_id} — `/streams` shows yours.",
        "ru": "❌ Не нахожу поток {stream_id} — `/streams` покажет ваши.",
    },
    "lifecycle_usage": {
        "en": "Which stream? Add its number — e.g. `/pausestream 3`. "
              "`/streams` shows the numbers.",
        "ru": "Какой поток? Добавьте его номер — например, `/pausestream 3`. "
              "Номера — в `/streams`.",
    },

    # ── /postsize ─────────────────────────────────────────────────────────
    "no_streams_yet": {
        "en": "You have no streams yet. Use `/newstream`.",
        "ru": "У вас пока нет потоков. Начните с `/newstream`.",
    },
    "postsize_usage": {
        "en": "Add the stream number — e.g. `/postsize 3` — and I'll show the "
              "options. `/streams` shows the numbers.",
        "ru": "Добавьте номер потока — например, `/postsize 3` — и я покажу "
              "варианты. Номера — в `/streams`.",
    },
    "postsize_pick": {
        "en": "Posts for \"{name}\" are currently \"{current}\". Pick a size:",
        "ru": "Посты потока \"{name}\" сейчас: \"{current}\". Выберите длину:",
    },
    "btn_standard": {
        "en": "📄 Standard (~100 words)",
        "ru": "📄 Обычные (~100 слов)",
    },
    "btn_compact": {
        "en": "⚡ Compact",
        "ru": "⚡ Короткие",
    },
    "plen_set_standard": {
        "en": "✅ Posts set to Standard (~100 words).",
        "ru": "✅ Посты будут обычной длины (~100 слов).",
    },
    "plen_set_compact": {
        "en": "✅ Posts set to Compact.",
        "ru": "✅ Посты будут короткими.",
    },

    # ── /language ─────────────────────────────────────────────────────────
    "lang_pick_ui": {
        "en": "Which language should I speak to you in?\n(Tip: `/language 3` "
              "sets the language of stream 3's POSTS.)",
        "ru": "На каком языке мне с вами говорить?\n(Подсказка: `/language 3` "
              "задаёт язык ПОСТОВ потока 3.)",
    },
    "lang_pick_stream": {
        "en": "Posts for \"{name}\" are currently in {current}. Pick a language:",
        "ru": "Посты потока \"{name}\" сейчас на языке: {current}. Выберите язык:",
    },
    "lang_ui_set": {
        "en": "✅ Got it — I'll speak English with you from now on.",
        "ru": "✅ Готово — теперь я говорю с вами по-русски.",
    },
    "lang_stream_set": {
        "en": "✅ Posts for \"{name}\" will be written in {language}.",
        "ru": "✅ Посты потока \"{name}\" будут на языке: {language}.",
    },
    "lang_name_en": {"en": "English", "ru": "английский"},
    "lang_name_ru": {"en": "Russian", "ru": "русский"},
    "btn_lang_en": {"en": "🇬🇧 English", "ru": "🇬🇧 English"},
    "btn_lang_ru": {"en": "🇷🇺 Русский", "ru": "🇷🇺 Русский"},

    # ── stream lifecycle ──────────────────────────────────────────────────
    "stream_paused": {
        "en": "⏸️ **{name}** is paused — no posts, and I stop checking its "
              "sites. `/resumestream {stream_id}` brings it back.",
        "ru": "⏸️ **{name}** на паузе — посты не приходят, сайты я не проверяю. "
              "Вернуть: `/resumestream {stream_id}`.",
    },
    "stream_resumed": {
        "en": "▶️ **{name}** is active again — I'll start checking its sites "
              "within half an hour.",
        "ru": "▶️ **{name}** снова активен — начну проверять его сайты в "
              "ближайшие полчаса.",
    },
    "delete_confirm": {
        "en": "Delete \"{name}\" and its {n} source subscription(s)? "
              "This can't be undone.",
        "ru": "Удалить \"{name}\" и его подписки на источники ({n} шт.)? "
              "Это действие необратимо.",
    },
    "btn_delete_yes": {"en": "🗑 Yes, delete it", "ru": "🗑 Да, удалить"},
    "btn_delete_keep": {"en": "✖️ Keep it", "ru": "✖️ Оставить"},
    "stream_deleted": {
        "en": "🗑 Deleted \"{name}\" and unsubscribed its sources.",
        "ru": "🗑 Поток \"{name}\" удалён, подписки на источники сняты.",
    },
    "kept_nothing_deleted": {
        "en": "Kept — nothing was deleted.",
        "ru": "Оставил — ничего не удалено.",
    },

    # ── /quiet ────────────────────────────────────────────────────────────
    "quiet_usage": {
        "en": "For example: `/quiet 3 23-8` — no posts from 23:00 to 08:00 "
              "(server time). `/quiet 3 off` turns quiet hours off. Easier "
              "with buttons: `/menu` → the stream → Quiet hours.",
        "ru": "Например: `/quiet 3 23-8` — без постов с 23:00 до 08:00 (время "
              "сервера). `/quiet 3 off` — отключить. Проще кнопками: `/menu` → "
              "поток → Тихие часы.",
    },
    "quiet_cleared": {
        "en": "🔔 Quiet hours cleared for **{name}** — posts flow 24/7.",
        "ru": "🔔 Тихие часы для **{name}** отключены — посты идут круглосуточно.",
    },
    "quiet_bad_spec": {
        "en": "That doesn't parse — use e.g. `23-8` (hours 0–23, start ≠ end).",
        "ru": "Не понял формат — напишите, например, `23-8` (часы 0–23, начало "
              "≠ конец).",
    },
    "quiet_set": {
        "en": "🔕 Quiet hours set for **{name}**: nothing between {start}:00 "
              "and {end}:00 (server time). Held posts go out on the first "
              "cycle after the window ends.",
        "ru": "🔕 Тихие часы для **{name}**: ничего с {start}:00 до {end}:00 "
              "(время сервера). Отложенные посты придут в первом цикле после "
              "окончания окна.",
    },

    # ── /streams ──────────────────────────────────────────────────────────
    "streams_none": {
        "en": "📭 You have no streams yet. Use `/newstream` to create one.",
        "ru": "📭 У вас пока нет потоков. Создайте первый: `/newstream`.",
    },
    "streams_header": {
        "en": "# 📋 Your News Streams\n\n| ID | Name | Status | Sources |\n"
              "|----|------|--------|---------|\n",
        "ru": "# 📋 Ваши новостные потоки\n\n| ID | Название | Статус | Источники |\n"
              "|----|----------|--------|-----------|\n",
    },
    "streams_footer": {
        "en": "\nUse `/sources <id>` to view sources for a stream.",
        "ru": "\n`/sources <id>` покажет источники потока.",
    },
    "status_active": {"en": "active", "ru": "активен"},
    "status_paused": {"en": "paused", "ru": "на паузе"},
    "status_researching": {"en": "researching", "ru": "идёт подбор"},

    # ── /sources ──────────────────────────────────────────────────────────
    "sources_usage": {
        "en": "Add the stream number — e.g. `/sources 3`. `/streams` shows "
              "the numbers.",
        "ru": "Добавьте номер потока — например, `/sources 3`. Номера — в "
              "`/streams`.",
    },
    "sources_none": {
        "en": "📭 No sources found for stream `{stream_id}`.",
        "ru": "📭 У потока `{stream_id}` нет источников.",
    },
    "sources_header": {
        "en": "# 📰 Sources for Stream `{stream_id}`\n\n"
              "| ID | Source | Score | Status |\n|----|--------|-------|--------|\n",
        "ru": "# 📰 Источники потока `{stream_id}`\n\n"
              "| ID | Источник | Оценка | Статус |\n|----|----------|--------|--------|\n",
    },
    "sources_details": {"en": "\n---\n## Details\n", "ru": "\n---\n## Подробнее\n"},
    "sources_site": {"en": "- Site: {url}", "ru": "- Сайт: {url}"},
    "sources_polling": {"en": "- Watching: {url}", "ru": "- Слежу за: {url}"},
    "sources_score_status": {
        "en": "- Score: {score}/100 · Status: {status}",
        "ru": "- Оценка: {score}/100 · Статус: {status}",
    },
    "sources_keywords": {"en": "- Keywords: {kw}", "ru": "- Ключевые слова: {kw}"},
    "sources_delete_hint": {
        "en": "\n\n_Delete with_ `/deletesource <ID>` _— or tap below._",
        "ru": "\n\n_Удалить: `/deletesource <ID>` — или нажмите кнопку ниже._",
    },
    "sources_tap_delete": {
        "en": "Tap a source to delete it:",
        "ru": "Нажмите на источник, чтобы удалить его:",
    },
    "type_news_site": {"en": "📰 News site", "ru": "📰 Новостной сайт"},
    "type_company_blog": {"en": "🏢 Company blog", "ru": "🏢 Блог компании"},
    "type_aggregator": {"en": "🔗 Aggregator", "ru": "🔗 Агрегатор"},
    "type_analysis": {"en": "🔬 Analysis", "ru": "🔬 Аналитика"},

    # ── /addsource ────────────────────────────────────────────────────────
    "addsource_usage": {
        "en": "Send the stream number and the site — e.g. "
              "`/addsource 3 techcrunch.com`.",
        "ru": "Пришлите номер потока и сайт — например, "
              "`/addsource 3 techcrunch.com`.",
    },
    "addsource_looking": {
        "en": "🔍 Looking for the news page on `{url}`...\n\n_This takes a "
              "moment._",
        "ru": "🔍 Ищу страницу новостей на `{url}`...\n\n_Это займёт немного "
              "времени._",
    },
    "addsource_inspect_error": {
        "en": "❌ I couldn't read that site right now — it may be down or "
              "refusing visitors. Try again a bit later.",
        "ru": "❌ Сейчас не получилось прочитать этот сайт — возможно, он не "
              "работает или не пускает. Попробуйте чуть позже.",
    },
    "addsource_none_found": {
        "en": "I looked all over <b>{url}</b> and couldn't find a page that "
              "lists its articles.\n\nThat usually means the site doesn't "
              "publish news, or it blocks automated readers.\n\nAdd it anyway "
              "and check the address as given?",
        "ru": "Я обошёл <b>{url}</b> целиком и не нашёл страницы со списком "
              "статей.\n\nОбычно это значит, что сайт не публикует новости или "
              "не пускает автоматических читателей.\n\nДобавить всё равно и "
              "проверять адрес как есть?",
    },
    "btn_add_anyway": {"en": "➕ Add anyway", "ru": "➕ Добавить всё равно"},
    "btn_cancel": {"en": "✖️ Cancel", "ru": "✖️ Отмена"},
    "addsource_multi": {
        "en": "<b>{url}</b> has more than one page that publishes articles."
              "\n\nWhich should I follow?",
        "ru": "На сайте <b>{url}</b> несколько страниц со статьями.\n\n"
              "За какой следить?",
    },
    "btn_feed_option": {
        "en": "{icon} {url} · {n} articles",
        "ru": "{icon} {url} · статей: {n}",
    },
    "source_added": {
        "en": "✅ **Source added** — {name}\n\n**Site:** {site}\n"
              "**Watching:** {poll}\n_Found its {kind} — {n} articles there now._"
              "\n\nFrom here on I'm watching it: everything already published "
              "stays quiet, and you'll only hear about what appears *after* "
              "this moment.",
        "ru": "✅ **Источник добавлен** — {name}\n\n**Сайт:** {site}\n"
              "**Слежу за:** {poll}\n_Нашёл {kind} — сейчас там статей: {n}._"
              "\n\nС этого момента я слежу за ним: всё уже опубликованное "
              "останется в тишине, а присылать буду только то, что появится "
              "*после*.",
    },
    "kind_feed": {"en": "RSS feed", "ru": "RSS-ленту"},
    "kind_page": {"en": "article page", "ru": "страницу статей"},
    "kind_page_ext": {
        "en": "page linking out to articles",
        "ru": "страницу со ссылками на статьи",
    },
    "source_dup": {
        "en": "⚠️ That source is already on this stream.",
        "ru": "⚠️ Этот источник уже есть в потоке.",
    },
    "added_anyway": {
        "en": "➕ **Added anyway**\n\nI'll check {url} as given. If it never "
              "shows anything readable, I'll stop checking it and mark it in "
              "your sources list.",
        "ru": "➕ **Добавлено как есть**\n\nБуду проверять {url} как есть. Если "
              "там так и не появится ничего читаемого, я перестану проверять и "
              "отмечу это в списке источников.",
    },
    "added_as_is": {"en": "Added as-is.", "ru": "Добавлено как есть."},
    "cancelled_nothing_added": {
        "en": "Cancelled — nothing was added.",
        "ru": "Отменено — ничего не добавлено.",
    },
    "choice_expired": {
        "en": "That choice expired. Run /addsource again.",
        "ru": "Выбор устарел. Запустите /addsource ещё раз.",
    },
    "option_invalid": {
        "en": "That option is no longer valid.",
        "ru": "Этот вариант больше не действителен.",
    },
    "following_page": {"en": "Following {url}.", "ru": "Слежу за {url}."},

    # ── /deletesource ─────────────────────────────────────────────────────
    "deletesource_usage": {
        "en": "Add the source number — e.g. `/deletesource 12`. The numbers "
              "are in `/sources`, or just use the buttons in `/menu`.",
        "ru": "Добавьте номер источника — например, `/deletesource 12`. Номера "
              "— в `/sources`, а проще — кнопками в `/menu`.",
    },
    "invalid_source_id": {
        "en": "I don't recognise that source number — `/sources 3` shows them, "
              "or use the buttons in `/menu`.",
        "ru": "Не узнаю этот номер источника — `/sources 3` покажет номера, а "
              "проще — кнопками в `/menu`.",
    },
    "source_not_found": {
        "en": "❌ I can't find source {source_id}. The numbers are in the "
              "first column of `/sources` — or just use the buttons in `/menu`.",
        "ru": "❌ Не нахожу источник {source_id}. Номера — в первой колонке "
              "`/sources`, а проще — кнопками в `/menu`.",
    },
    "source_not_on_streams": {
        "en": "❌ That source isn't on any of your streams.",
        "ru": "❌ Этот источник не привязан к вашим потокам.",
    },
    "source_removed": {
        "en": "🗑️ Removed source `{source_id}` — {name}",
        "ru": "🗑️ Источник `{source_id}` удалён — {name}",
    },
    "deleted_short": {"en": "🗑️ Deleted.", "ru": "🗑️ Удалено."},
    "delete_failed_short": {
        "en": "Couldn't delete that one.",
        "ru": "Не получилось удалить.",
    },

    # ── /latest ───────────────────────────────────────────────────────────
    "latest_none": {
        "en": "📭 No articles fetched yet.",
        "ru": "📭 Статей пока нет.",
    },
    "latest_header": {
        "en": "# 📰 Latest Articles\n\n| # | Title | Source | Relevance |\n"
              "|---|-------|--------|----------|\n",
        "ru": "# 📰 Последние статьи\n\n| # | Заголовок | Источник | Релевантность |\n"
              "|---|-----------|----------|---------------|\n",
    },
}


def t(lang: str, key: str, **fmt) -> str:
    """Render `key` in `lang` (falls back to English), with {placeholders}."""
    entry = STRINGS.get(key)
    if entry is None:
        logger.error("i18n: unknown string key %r", key)
        return key
    text = entry.get(lang) or entry[DEFAULT_LANG]
    if fmt:
        try:
            return text.format(**fmt)
        except (KeyError, IndexError):
            logger.exception("i18n: bad placeholders for key %r", key)
            return text
    return text
