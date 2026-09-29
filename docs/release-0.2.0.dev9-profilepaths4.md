# Dev9.4 provider-profile path fix

- Recognizes Zocdoc provider profile URLs beyond `/doctor/...`, including `/dentist/...`.
- Listing readiness now counts `doctor-card-info-name` anchors regardless of profile path.
- Durable listing trace accepts provider-type profile paths.
- Profile queue/export accepts provider-type profile paths.
- Chrome capture recognizes provider-type profile pages.
- Retained `trace_partial` Dentist pagination HTML can be repaired offline with `repair-trace-partials`.
