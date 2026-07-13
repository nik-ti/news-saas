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
    # ── /start ────────────────────────────────────────────────────────────
    "start_user": {
        "en": """\
# 🗞️ NewsStream Bot

*Your personal news researcher.*

Tell me what you want to stay on top of — in your own words — and I'll find \
the best sources for it, watch them around the clock, and send you only the \
stories that match.

**Start with** `/newstream` — just describe your topic.

## Your commands

| Command | Description |
|---------|-------------|
| `/newstream` | Set up a news stream (just tell me what you want) |
| `/streams` | List your streams |
| `/sources <stream_id>` | See what a stream is watching |
| `/addsource <stream_id> <url>` | Add a site you already like |
| `/deletesource <source_id>` | Remove a source from your stream |
| `/research <stream_id>` | Re-run source research for a stream |
| `/latest` | Your latest fetched articles |
| `/postsize <stream_id>` | Post length: standard / compact |
| `/language` | Bot language · `/language <stream_id>` — post language |
| `/pausestream <stream_id>` | Pause a stream |
| `/resumestream <stream_id>` | Resume a paused stream |
| `/deletestream <stream_id>` | Delete a stream |
| `/quiet <stream_id> 23-8` | No posts between those hours (`off` to clear) |\
""",
        "ru": """\
# 🗞️ NewsStream Bot

*Ваш личный новостной ассистент.*

Расскажите своими словами, за чем хотите следить, — я найду лучшие источники, \
буду наблюдать за ними круглосуточно и присылать только те новости, которые \
вам действительно нужны.

**Начните с** `/newstream` — просто опишите тему.

## Ваши команды

| Команда | Описание |
|---------|----------|
| `/newstream` | Создать новостной поток (просто расскажите, что нужно) |
| `/streams` | Список ваших потоков |
| `/sources <stream_id>` | Источники потока |
| `/addsource <stream_id> <url>` | Добавить свой сайт |
| `/deletesource <source_id>` | Убрать источник из потока |
| `/research <stream_id>` | Заново подобрать источники |
| `/latest` | Последние собранные статьи |
| `/postsize <stream_id>` | Длина постов: обычная / короткая |
| `/language` | Язык бота · `/language <stream_id>` — язык постов |
| `/pausestream <stream_id>` | Приостановить поток |
| `/resumestream <stream_id>` | Возобновить поток |
| `/deletestream <stream_id>` | Удалить поток |
| `/quiet <stream_id> 23-8` | Тихие часы — без постов в это время (`off` — отключить) |\
""",
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
        "en": "\n⏳ {n} listed no articles on the first read — they stay on "
              "watch and count as live the moment they yield.",
        "ru": "\n⏳ {n} при первой проверке не показали статей — они остаются "
              "под наблюдением и включатся, как только что-то появится.",
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
        "en": "\nWant to fine-tune? `/sources {stream_id}` shows the full list "
              "— drop any with `/deletesource <id>` or add your own with "
              "`/addsource {stream_id} <url>`.",
        "ru": "\nХотите настроить точнее? `/sources {stream_id}` покажет весь "
              "список — уберите лишнее через `/deletesource <id>` или добавьте "
              "своё: `/addsource {stream_id} <url>`.",
    },
    "res_none": {
        "en": "I dug through a lot of sites but couldn't find sources solid "
              "enough to trust for this one yet — often that means the topic "
              "is very narrow, or worth phrasing a little differently.\n\n"
              "A couple of options:\n"
              "• `/research {stream_id}` — I'll take another pass at it.\n"
              "• `/addsource {stream_id} <url>` — point me at a site you "
              "already like and I'll build from there.",
        "ru": "Я просмотрел много сайтов, но пока не нашёл достаточно "
              "надёжных источников — обычно это значит, что тема очень узкая "
              "или её стоит сформулировать чуть иначе.\n\nВарианты:\n"
              "• `/research {stream_id}` — попробую ещё раз.\n"
              "• `/addsource {stream_id} <url>` — покажите сайт, который вам "
              "нравится, и я оттолкнусь от него.",
    },
    "research_error": {
        "en": "❌ Research error: {e}",
        "ru": "❌ Ошибка при подборе источников: {e}",
    },
    "research_usage": {
        "en": "Usage: `/research <stream_id>`",
        "ru": "Формат: `/research <stream_id>`",
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
        "en": "Invalid stream ID.",
        "ru": "Некорректный ID потока.",
    },
    "not_your_stream": {
        "en": "❌ That stream isn't yours.",
        "ru": "❌ Это не ваш поток.",
    },
    "stream_not_found": {
        "en": "❌ Stream `{stream_id}` not found.",
        "ru": "❌ Поток `{stream_id}` не найден.",
    },
    "lifecycle_usage": {
        "en": "Usage: give me a stream id — `/streams` lists yours.",
        "ru": "Укажите ID потока — `/streams` покажет ваши.",
    },

    # ── /postsize ─────────────────────────────────────────────────────────
    "no_streams_yet": {
        "en": "You have no streams yet. Use `/newstream`.",
        "ru": "У вас пока нет потоков. Начните с `/newstream`.",
    },
    "postsize_usage": {
        "en": "Usage: `/postsize <stream_id>` — I'll show the options.",
        "ru": "Формат: `/postsize <stream_id>` — покажу варианты.",
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
        "en": "Which language should I speak to you in?\n(Tip: `/language "
              "<stream_id>` sets the language of a stream's POSTS.)",
        "ru": "На каком языке мне с вами говорить?\n(Подсказка: `/language "
              "<stream_id>` задаёт язык ПОСТОВ конкретного потока.)",
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
        "en": "⏸️ **{name}** is paused — nothing will be posted and its "
              "sources stop being crawled. `/resumestream {stream_id}` brings "
              "it back.",
        "ru": "⏸️ **{name}** на паузе — посты не приходят, источники не "
              "проверяются. Вернуть: `/resumestream {stream_id}`.",
    },
    "stream_resumed": {
        "en": "▶️ **{name}** is active again. Sources resume on the next cycle.",
        "ru": "▶️ **{name}** снова активен. Источники включатся со следующего "
              "цикла.",
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
        "en": "Usage: `/quiet <stream_id> 23-8` — no posts from 23:00 to 08:00 "
              "(server time). `/quiet <stream_id> off` clears it.",
        "ru": "Формат: `/quiet <stream_id> 23-8` — без постов с 23:00 до 08:00 "
              "(время сервера). `/quiet <stream_id> off` — отключить.",
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
        "en": "Usage: `/sources <stream_id>`",
        "ru": "Формат: `/sources <stream_id>`",
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
    "sources_polling": {"en": "- Polling: {url}", "ru": "- Опрашивается: {url}"},
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
        "en": "Usage: `/addsource <stream_id> <url>`",
        "ru": "Формат: `/addsource <stream_id> <url>`",
    },
    "addsource_looking": {
        "en": "🔍 Looking for the news page on `{url}`...\n\n_Checking for "
              "feeds, section pages, and site navigation. This takes a moment._",
        "ru": "🔍 Ищу страницу новостей на `{url}`...\n\n_Проверяю ленты, "
              "разделы и навигацию сайта. Это займёт немного времени._",
    },
    "addsource_inspect_error": {
        "en": "❌ Couldn't inspect that site: {e}",
        "ru": "❌ Не удалось изучить этот сайт: {e}",
    },
    "addsource_none_found": {
        "en": "I crawled <b>{url}</b> — its feeds, common news paths, and its "
              "own navigation — and couldn't find any page that lists articles."
              "\n\nThat usually means the site doesn't publish a news feed, or "
              "it blocks crawlers.\n\nAdd it anyway and poll the URL as given?",
        "ru": "Я обошёл <b>{url}</b> — ленты, типовые разделы новостей и "
              "навигацию — и не нашёл страницы со списком статей.\n\nОбычно "
              "это значит, что у сайта нет новостной ленты или он блокирует "
              "роботов.\n\nДобавить всё равно и опрашивать адрес как есть?",
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
        "en": "✅ **Source added** — `{source_id}`\n\n**Site:** {site}\n"
              "**Polling:** {poll}\n_Found via {kind} — {n} articles detected._"
              "\n\nI'll baseline it on the next cycle: everything published so "
              "far is recorded silently, and you'll only hear about what "
              "appears *after* that.",
        "ru": "✅ **Источник добавлен** — `{source_id}`\n\n**Сайт:** {site}\n"
              "**Опрашивается:** {poll}\n_Найдено через {kind} — статей: {n}._"
              "\n\nВ следующем цикле я сниму «нулевую точку»: всё уже "
              "опубликованное запишется молча, а присылать буду только то, "
              "что появится *после*.",
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
        "en": "➕ **Added anyway** — `{source_id}`\n\nI'll poll {url} directly. "
              "If it never yields articles it will be marked as errored after "
              "a few cycles.",
        "ru": "➕ **Добавлено как есть** — `{source_id}`\n\nБуду опрашивать "
              "{url} напрямую. Если статей так и не появится, через несколько "
              "циклов источник будет помечен как нерабочий.",
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
        "en": "Usage: `/deletesource <source_id>`",
        "ru": "Формат: `/deletesource <source_id>`",
    },
    "invalid_source_id": {
        "en": "Invalid source ID.",
        "ru": "Некорректный ID источника.",
    },
    "source_not_found": {
        "en": "❌ No source with ID `{source_id}`.\n\nUse `/sources "
              "<stream_id>` — the **ID** column is what you pass here, not the "
              "row position.",
        "ru": "❌ Источника с ID `{source_id}` нет.\n\nОткройте `/sources "
              "<stream_id>` — сюда передаётся значение из колонки **ID**, а не "
              "номер строки.",
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
