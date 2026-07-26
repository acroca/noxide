# Vault schema

The raw-journal + compiled-wiki design and its operations (ingest/query/compile/lint) are built into the assistant. This file adds only what the code cannot know: who the vault's owner is, what language to use, and any local conventions. It comes after the built-in prompt and takes precedence over it — so it can also override built-in behavior when needed. If a convention here repeatedly causes friction, propose an amendment instead of silently deviating.

## Language

<!-- Reply language, vault-content language, system-file language.
     Delete this section if everything is English. Example:
     **Always communicate in Spanish** — replies, Telegram messages, scheduled job
     messages. Vault content stays in the language the user used. System files
     (`AGENTS.md`, topic prompts) are written in English. -->

## User profile

<!-- Name, date of birth, location, timezone. The timezone drives the "local time
     in everything the user reads" rule. -->

## Localized terms

<!-- Only if the vault is not kept in English: the exact localized forms, mapped to
     the built-in English defaults so operations apply unambiguously. Example:
     - Routines table header: `| Rutina | Frecuencia | Última vez | Próxima | Notas |`
       (= Routine / Frequency / Last done / Next due / Notes)
     - `now.md` sections: **Hoy** (= Today), **Próximo** (= Upcoming), …
     - Status label: `**Estado:**`; tasks: `(para …)` / `(hecho …)` / `(esperando: X)`
     Localize the words, never the date format: the argument of a task marker stays
     ISO (`(para 2026-08-12)`), and so does every other date in the vault. Weekday
     names in `now.md` are user-visible text and do localize. -->

## Local conventions

<!-- Anything this vault does differently or in addition: extra directories
     (e.g. a raw/legacy/ with pre-migration files), extra page fields, preferred
     follow-up times, … Delete if empty. -->
