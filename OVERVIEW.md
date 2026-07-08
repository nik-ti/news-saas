
**Concept:** A self-serve service where users describe a topic they're interested in in plain language, and the system automatically:

1. Researches and builds a database of relevant news sources for that topic
2. Runs them through a pre-built pipeline (fetch → summarize → relevance check → dedupe → write post)
3. Delivers a personalized news feed to the user's preferred channel (Telegram, Discord, email, etc)

**Users can:**
1. Describe the desires news, no matter how specific. 
2. Tune the type of news they want, as they go. 
3. Set preferred tone, writing style, language, etc. 

**Use case:** People either just read it as a digest, or use it as raw content input for their own business.

**Angle:** The right information is the #1 resource. The user can create his own stream of high-important news, without needing to check multiple sources (News sites, telegram channels, X.com, YT) and spend time finding them in the first place. 

**How it works:**
1. User is guided through a step of questions, to gather the needed info.
2. Our system then performs a deep, AI powered research to find the best matching sources, which will include news sites, and x.com accounts, potentially. 
3. News are then also filtered through the defined criteria, deterministically. 
4. User can view, remove, add the sources directly, or by describing what he doesn't like in the kind of news he gets. 

**Key things:** 
1. The autonomous research is the hardest part. The found sources must truly match the kind of news a user wants, no matter how specific. That means, a multi-agentic system, that analyzes the news in each source candidate, and is not 'satisfied' by bare minimum relevance.  
2. As more users use the service, our system will add each found source to an internal database, consisting of the exact source, its broad news category, and description/keywords for the more specific details (what exact kind of news they focus on, types of articles, etc). That way, every research for a new user, will start with checking our internal db first. 

--—


## Features / Ideas
1. Users can select the news format that they receive. For example, only headlines, a few sentence summaries, or the entire articles. They can also select whether or not they want the source article url attached. 
2. An option (premium potentially) for a fact check (news received -> research -> article + research sent). Maybe a button - “Research”. 
3. If a specific source blocks our crawler, we can make it as a “premium source”, and use firecrawl 
4. Have ready-to-go streams. User can just choose it and start receiving the relevant news. For example: crypto news, geopolitics, real estate news in CA, soccer news in Europe, etc. 

## Potential issues
1. Have to minimize the resource consumption by the web crawler, given that it will work often. I have used it before, and sometimes if the headless browser stays open, it will consume lots of memory.


## Plan

We should start with an MVP, in a form of a telegram bot, where we can test the most tricky part of this service - research engine. Telegram bot will essentially allow users to do everything above, but in a simplified way. User can add a new type of news, and answer some questions in regards to it. Say a few premade questions, then we feed those to an LLM, which knows about our research protocol (to be built), and deterministically will create follow up questions for this user to help the research. After all questions are answered, the research begins. Research must consist of multiple processes, for example, one or more agents search for relevant news pages (later also rss feeds, x.com accounts, and more), others will then do a more thorough analysis of each source, by examining the kind of articles they post, and wether they match the user's criteria (which may or may not be strict). This is just my best way of thinking about this, but im open to improvements. 

For the MVP, that will be all. After sources are found (3-8), user can see those sources, manually delete or add them. Anytime a new source is added, wether by the user, or through the research, it must be tested with our webcrawler to make sure the content is fetchable and we are not blocked. Can use proxy if having hard time. 