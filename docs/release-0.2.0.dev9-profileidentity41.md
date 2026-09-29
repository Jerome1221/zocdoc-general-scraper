# Dev9.4.1 Profile Identity Hotfix

This hotfix removes the remaining assumption that every Zocdoc provider profile URL ends in a numeric id.

Observed valid provider-card URL shapes include:

- `/dentist/aaron-wildung-dds-409823`
- `/dentist/amy-hartsfield-dmd`
- `/doctor/1114140902-timothy-nettles-dmd`

`is_profile_url()` now accepts same-origin two-segment provider-card paths while excluding known non-profile route families and listing-location slugs ending in `<id>pm`.

This aligns listing validation, durable trace persistence, and profile queueing for providers whose public URL does not carry a trailing numeric Zocdoc id.
