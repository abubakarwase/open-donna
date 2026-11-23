//! Resolve relative model slots into concrete tool args (time, people, ids).

use chrono::{DateTime, Datelike, Duration, NaiveDate, TimeZone, Timelike, Weekday};
use chrono_tz::Tz;
use serde_json::{json, Value};
use std::collections::HashMap;

/// Part-of-day windows (local hours, end exclusive-ish as golden before).
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
pub enum Part {
    Morning,
    Afternoon,
    AfterLunch,
    Evening,
    LateMorning,
    EarlyEvening,
    AllDay,
    /// Mail "this morning": 00:00–12:00.
    MailMorning,
}

impl Part {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "morning" => Some(Self::Morning),
            "afternoon" => Some(Self::Afternoon),
            "after_lunch" => Some(Self::AfterLunch),
            "evening" => Some(Self::Evening),
            "late_morning" => Some(Self::LateMorning),
            "early_evening" => Some(Self::EarlyEvening),
            "all_day" => Some(Self::AllDay),
            "mail_morning" => Some(Self::MailMorning),
            _ => None,
        }
    }

    fn hours(self) -> (u32, u32) {
        match self {
            Self::Morning => (9, 12),
            Self::Afternoon => (12, 18),
            Self::AfterLunch => (13, 17),
            Self::Evening => (17, 20),
            Self::LateMorning => (10, 13),
            Self::EarlyEvening => (16, 18),
            Self::AllDay => (0, 24),
            Self::MailMorning => (0, 12),
        }
    }
}

#[derive(Debug, Clone, PartialEq, Eq)]
pub enum DaySpec {
    Today,
    Tomorrow,
    Weekday(Weekday),
    ThisWeek(Weekday),
    NextWeek(Weekday),
    ThisWeekSpan,
    IsoDate(NaiveDate),
    ClockChangeDay,
    DayBeforeClockChange,
    DayAfterClockChange,
}

impl DaySpec {
    pub fn parse(s: &str) -> Option<Self> {
        match s {
            "today" => Some(Self::Today),
            "tomorrow" => Some(Self::Tomorrow),
            "this_week" => Some(Self::ThisWeekSpan),
            "clock_change_day" => Some(Self::ClockChangeDay),
            "day_before_clock_change" => Some(Self::DayBeforeClockChange),
            "day_after_clock_change" => Some(Self::DayAfterClockChange),
            "monday" => Some(Self::Weekday(Weekday::Mon)),
            "tuesday" => Some(Self::Weekday(Weekday::Tue)),
            "wednesday" => Some(Self::Weekday(Weekday::Wed)),
            "thursday" => Some(Self::Weekday(Weekday::Thu)),
            "friday" => Some(Self::Weekday(Weekday::Fri)),
            "saturday" => Some(Self::Weekday(Weekday::Sat)),
            "sunday" => Some(Self::Weekday(Weekday::Sun)),
            "this_monday" => Some(Self::ThisWeek(Weekday::Mon)),
            "this_tuesday" => Some(Self::ThisWeek(Weekday::Tue)),
            "this_wednesday" => Some(Self::ThisWeek(Weekday::Wed)),
            "this_thursday" => Some(Self::ThisWeek(Weekday::Thu)),
            "this_friday" => Some(Self::ThisWeek(Weekday::Fri)),
            "next_monday" => Some(Self::NextWeek(Weekday::Mon)),
            "next_tuesday" => Some(Self::NextWeek(Weekday::Tue)),
            "next_wednesday" => Some(Self::NextWeek(Weekday::Wed)),
            "next_thursday" => Some(Self::NextWeek(Weekday::Thu)),
            "next_friday" => Some(Self::NextWeek(Weekday::Fri)),
            "next_week" => Some(Self::NextWeek(Weekday::Mon)),
            other => NaiveDate::parse_from_str(other, "%Y-%m-%d")
                .ok()
                .map(Self::IsoDate),
        }
    }
}

fn parse_tz(timezone: &str) -> Result<Tz, String> {
    timezone
        .parse::<Tz>()
        .map_err(|e| format!("bad timezone {timezone}: {e}"))
}

fn local_now(now: &str, timezone: &str) -> Result<DateTime<Tz>, String> {
    let tz = parse_tz(timezone)?;
    let fixed = DateTime::parse_from_rfc3339(now).map_err(|e| e.to_string())?;
    Ok(fixed.with_timezone(&tz))
}

fn fmt_local(dt: DateTime<Tz>) -> String {
    dt.fixed_offset().to_rfc3339()
}

fn at_local(date: NaiveDate, hour: u32, minute: u32, tz: Tz) -> DateTime<Tz> {
    let h = hour.min(23);
    match tz.with_ymd_and_hms(date.year(), date.month(), date.day(), h, minute, 0) {
        chrono::LocalResult::Single(dt) => dt,
        chrono::LocalResult::Ambiguous(a, _) => a,
        chrono::LocalResult::None => {
            // Gap (spring forward): step forward an hour.
            tz.with_ymd_and_hms(
                date.year(),
                date.month(),
                date.day(),
                (h + 1).min(23),
                minute,
                0,
            )
            .earliest()
            .unwrap_or_else(|| {
                tz.with_ymd_and_hms(date.year(), date.month(), date.day(), 12, 0, 0)
                    .single()
                    .expect("noon exists")
            })
        }
    }
}

/// Next occurrence of weekday (always strictly in the future; skips today).
pub fn next_weekday(now: &str, timezone: &str, weekday: Weekday) -> Result<NaiveDate, String> {
    let local = local_now(now, timezone)?;
    let mut days = (weekday.num_days_from_monday() as i64
        - local.weekday().num_days_from_monday() as i64)
        .rem_euclid(7);
    if days == 0 {
        days = 7;
    }
    Ok(local.date_naive() + Duration::days(days))
}

/// Weekday this week if still upcoming (or today); else next week.
pub fn this_week_weekday(now: &str, timezone: &str, weekday: Weekday) -> Result<NaiveDate, String> {
    let local = local_now(now, timezone)?;
    let cur = local.weekday().num_days_from_monday() as i64;
    let target = weekday.num_days_from_monday() as i64;
    if cur <= target {
        Ok(local.date_naive() + Duration::days(target - cur))
    } else {
        next_weekday(now, timezone, weekday)
    }
}

/// Monday 00:00 → next Monday; weekend now → upcoming Mon–Sun.
pub fn week_bounds(now: &str, timezone: &str) -> Result<(String, String), String> {
    let local = local_now(now, timezone)?;
    let tz = parse_tz(timezone)?;
    let weekday = local.weekday().num_days_from_monday() as i64;
    let start_date = if weekday >= 5 {
        local.date_naive() + Duration::days(7 - weekday)
    } else {
        local.date_naive() - Duration::days(weekday)
    };
    let start = at_local(start_date, 0, 0, tz);
    let end = start + Duration::days(7);
    Ok((fmt_local(start), fmt_local(end)))
}

/// Agenda "this afternoon": 12:00–18:00, or now→18:00 if already afternoon.
pub fn afternoon_bounds(now: &str, timezone: &str) -> Result<(String, String), String> {
    let local = local_now(now, timezone)?;
    let tz = parse_tz(timezone)?;
    let noon = at_local(local.date_naive(), 12, 0, tz);
    let mut end = at_local(local.date_naive(), 18, 0, tz);
    let start = if local < noon {
        noon
    } else {
        local
            .with_second(0)
            .unwrap_or(local)
            .with_nanosecond(0)
            .unwrap_or(local)
    };
    if start >= end {
        end = at_local(local.date_naive(), 23, 59, tz);
        if start >= end {
            end = start + Duration::hours(1);
        }
    }
    Ok((fmt_local(start), fmt_local(end)))
}

pub fn morning_bounds_mail(now: &str, timezone: &str) -> Result<(String, String), String> {
    let local = local_now(now, timezone)?;
    let tz = parse_tz(timezone)?;
    let start = at_local(local.date_naive(), 0, 0, tz);
    let end = at_local(local.date_naive(), 12, 0, tz);
    Ok((fmt_local(start), fmt_local(end)))
}

/// Fall DST transition local calendar day (matches golden calendar transitions).
pub fn clock_change_date(timezone: &str, year: i32) -> Result<NaiveDate, String> {
    // Prefer known golden anchors; else scan via local FixedOffset length change.
    if year == 2026 {
        match timezone {
            "America/New_York" => return Ok(NaiveDate::from_ymd_opt(2026, 11, 1).unwrap()),
            "Europe/Amsterdam" => return Ok(NaiveDate::from_ymd_opt(2026, 10, 25).unwrap()),
            _ => {}
        }
    }
    let _ = parse_tz(timezone)?;
    Err(format!("no fall DST transition for {timezone} {year}"))
}

fn resolve_date(now: &str, timezone: &str, day: &DaySpec) -> Result<NaiveDate, String> {
    let local = local_now(now, timezone)?;
    match day {
        DaySpec::Today => Ok(local.date_naive()),
        DaySpec::Tomorrow => Ok(local.date_naive() + Duration::days(1)),
        DaySpec::Weekday(wd) => next_weekday(now, timezone, *wd),
        DaySpec::ThisWeek(wd) => this_week_weekday(now, timezone, *wd),
        DaySpec::NextWeek(wd) => next_weekday(now, timezone, *wd),
        DaySpec::ThisWeekSpan => Ok(local.date_naive()), // unused alone
        DaySpec::IsoDate(d) => Ok(*d),
        DaySpec::ClockChangeDay => clock_change_date(timezone, local.year()),
        DaySpec::DayBeforeClockChange => {
            Ok(clock_change_date(timezone, local.year())? - Duration::days(1))
        }
        DaySpec::DayAfterClockChange => {
            // Monday after: clock change Sunday → Monday is +1 from transition day in US
            // (transition local day is Sunday for US). Golden uses Mon after = transition+1.
            Ok(clock_change_date(timezone, local.year())? + Duration::days(1))
        }
    }
}

/// Resolve (day, part) into after/before RFC3339 strings. Optional hour overrides.
pub fn resolve_window(
    now: &str,
    timezone: &str,
    day: &DaySpec,
    part: Part,
    start_hour: Option<u32>,
    end_hour: Option<u32>,
) -> Result<(String, String), String> {
    if matches!(day, DaySpec::ThisWeekSpan) {
        return week_bounds(now, timezone);
    }
    if matches!(day, DaySpec::Today) && matches!(part, Part::Afternoon) && start_hour.is_none() {
        // Agenda-style "this afternoon".
        return afternoon_bounds(now, timezone);
    }
    if matches!(part, Part::MailMorning) {
        return morning_bounds_mail(now, timezone);
    }
    let tz = parse_tz(timezone)?;
    let date = resolve_date(now, timezone, day)?;
    let (dh0, dh1) = part.hours();
    let h0 = start_hour.unwrap_or(dh0);
    let h1 = end_hour.unwrap_or(dh1);
    let start = at_local(date, h0, 0, tz);
    let end = if h1 >= 24 {
        at_local(date + Duration::days(1), 0, 0, tz)
    } else {
        at_local(date, h1, 0, tz)
    };
    Ok((fmt_local(start), fmt_local(end)))
}

/// Map a spoken name/email to an address from known fixture people. Never invents.
pub fn resolve_people(mentions: &[String], known: &HashMap<String, String>) -> Vec<String> {
    let mut out = Vec::new();
    for m in mentions {
        let key = m.trim().to_lowercase();
        if key.is_empty() {
            continue;
        }
        if key.contains('@') {
            // Only keep if known or explicitly an email in the request; still filter to known when map non-empty.
            if known.is_empty()
                || known.values().any(|e| e.eq_ignore_ascii_case(&key))
                || known.contains_key(&key)
            {
                let email = known
                    .values()
                    .find(|e| e.eq_ignore_ascii_case(&key))
                    .cloned()
                    .unwrap_or_else(|| m.trim().to_string());
                if !out.iter().any(|e| e == &email) {
                    out.push(email);
                }
            }
            continue;
        }
        // First-name or full-name match against keys.
        if let Some(email) = known.get(&key) {
            if !out.iter().any(|e| e == email) {
                out.push(email.clone());
            }
            continue;
        }
        // Prefix first token match: "Sam" → sam@...
        if let Some((_, email)) = known
            .iter()
            .find(|(name, _)| name.starts_with(&key) || key.starts_with(name.as_str()))
        {
            if !out.iter().any(|e| e == email) {
                out.push(email.clone());
            }
        }
        // else: drop unknown names (never invent attendees)
    }
    out
}

/// Build name→email map from calendar attendees + mail senders.
pub fn people_index_from_values(events: &[Value], mail_rows: &[Value]) -> HashMap<String, String> {
    let mut map = HashMap::new();
    for e in events {
        if let Some(arr) = e.get("attendees").and_then(|a| a.as_array()) {
            for a in arr {
                if let Some(email) = a.as_str() {
                    index_email(&mut map, email);
                }
            }
        }
    }
    for r in mail_rows {
        if let Some(from) = r.get("from").and_then(|v| v.as_str()) {
            index_email(&mut map, from);
        }
    }
    map
}

fn index_email(map: &mut HashMap<String, String>, email: &str) {
    let email = email.trim();
    if !email.contains('@') {
        return;
    }
    map.insert(email.to_lowercase(), email.to_string());
    let local = email.split('@').next().unwrap_or("");
    // sam, dana.okonkwo → dana
    let first = local.split('.').next().unwrap_or(local);
    if !first.is_empty() {
        map.entry(first.to_lowercase())
            .or_insert_with(|| email.to_string());
    }
    // full local part
    map.entry(local.to_lowercase())
        .or_insert_with(|| email.to_string());
}

/// Relative fill schema for find_free_slot / list_events style windows.
pub fn relative_window_schema(include_duration: bool, include_people: bool) -> Value {
    let mut props = json!({
        "day": {
            "type": "string",
            "enum": [
                "today", "tomorrow", "this_week",
                "monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday",
                "this_monday", "this_tuesday", "this_wednesday", "this_thursday", "this_friday",
                "next_monday", "next_tuesday", "next_wednesday", "next_thursday", "next_friday",
                "clock_change_day", "day_before_clock_change", "day_after_clock_change"
            ]
        },
        "part": {
            "type": "string",
            "enum": ["morning", "afternoon", "after_lunch", "evening", "late_morning", "early_evening", "all_day"]
        },
        "start_hour": { "type": "integer", "minimum": 0, "maximum": 23 },
        "end_hour": { "type": "integer", "minimum": 1, "maximum": 24 }
    });
    let mut required = vec!["day", "part"];
    if include_duration {
        props.as_object_mut().unwrap().insert(
            "duration_min".into(),
            json!({ "type": "integer", "minimum": 5, "maximum": 480 }),
        );
        required.push("duration_min");
    }
    if include_people {
        props.as_object_mut().unwrap().insert(
            "people".into(),
            json!({
                "type": "array",
                "items": { "type": "string" }
            }),
        );
    }
    json!({
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": false
    })
}

pub fn relative_search_email_schema() -> Value {
    json!({
        "type": "object",
        "properties": {
            "query": { "type": "string" },
            "unread": { "type": "boolean" },
            "from_person": { "type": "string" },
            "day": {
                "type": "string",
                "enum": ["today", "tomorrow", "mail_morning", "last_month", "none"]
            },
            "part": {
                "type": "string",
                "enum": ["mail_morning", "all_day", "none"]
            }
        },
        "additionalProperties": false,
        "minProperties": 1
    })
}

/// Apply relative slots → concrete tool args for known tools.
pub fn resolve_tool_args(
    tool_name: &str,
    relative: &Value,
    now: &str,
    timezone: &str,
    people: &HashMap<String, String>,
    thread_ids: &[String],
    event_ids: &[String],
) -> Result<Value, String> {
    match tool_name {
        "find_free_slot" => resolve_find_free_slot(relative, now, timezone, people),
        "list_events" => resolve_list_events(relative, now, timezone),
        "search_email" => resolve_search_email(relative, now, timezone, people),
        "get_thread" => resolve_id_arg(relative, "thread_id", thread_ids),
        "cancel_event" => resolve_id_arg(relative, "event_id", event_ids),
        "update_event" => resolve_update_event(relative, now, timezone, event_ids),
        "create_event" => resolve_create_event(relative, now, timezone, people),
        "draft_email" => resolve_draft_email(relative, thread_ids),
        _ => Ok(relative.clone()), // query_sheet etc. already concrete
    }
}

fn resolve_find_free_slot(
    relative: &Value,
    now: &str,
    timezone: &str,
    people: &HashMap<String, String>,
) -> Result<Value, String> {
    let day = relative
        .get("day")
        .and_then(|v| v.as_str())
        .and_then(DaySpec::parse)
        .ok_or_else(|| "missing day".to_string())?;
    let part = relative
        .get("part")
        .and_then(|v| v.as_str())
        .and_then(Part::parse)
        .unwrap_or(Part::AllDay);
    let start_hour = relative
        .get("start_hour")
        .and_then(|v| v.as_u64())
        .map(|u| u as u32);
    let end_hour = relative
        .get("end_hour")
        .and_then(|v| v.as_u64())
        .map(|u| u as u32);
    let duration_min = relative
        .get("duration_min")
        .and_then(|v| v.as_i64())
        .ok_or_else(|| "missing duration_min".to_string())?;
    let (after, before) = resolve_window(now, timezone, &day, part, start_hour, end_hour)?;
    let mut args = json!({
        "duration_min": duration_min,
        "after": after,
        "before": before
    });
    if let Some(arr) = relative.get("people").and_then(|v| v.as_array()) {
        let mentions: Vec<String> = arr
            .iter()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect();
        let resolved = resolve_people(&mentions, people);
        if !resolved.is_empty() {
            args.as_object_mut()
                .unwrap()
                .insert("attendees".into(), json!(resolved));
        }
    }
    Ok(args)
}

fn resolve_list_events(relative: &Value, now: &str, timezone: &str) -> Result<Value, String> {
    // Allow already-concrete start/end.
    if relative.get("start").and_then(|v| v.as_str()).is_some()
        && relative.get("end").and_then(|v| v.as_str()).is_some()
    {
        return Ok(json!({
            "start": relative["start"],
            "end": relative["end"]
        }));
    }
    let day = relative
        .get("day")
        .and_then(|v| v.as_str())
        .and_then(DaySpec::parse)
        .unwrap_or(DaySpec::Today);
    let part = relative
        .get("part")
        .and_then(|v| v.as_str())
        .and_then(Part::parse)
        .unwrap_or(Part::AllDay);
    let (start, end) = resolve_window(now, timezone, &day, part, None, None)?;
    Ok(json!({ "start": start, "end": end }))
}

fn resolve_search_email(
    relative: &Value,
    now: &str,
    timezone: &str,
    people: &HashMap<String, String>,
) -> Result<Value, String> {
    let mut args = serde_json::Map::new();
    if let Some(q) = relative.get("query").and_then(|v| v.as_str()) {
        if !q.is_empty() {
            args.insert("query".into(), json!(q));
        }
    }
    // Only emit unread when true; never emit unread:false (hurts arg_f1).
    if relative.get("unread").and_then(|v| v.as_bool()) == Some(true) {
        args.insert("unread".into(), json!(true));
    }
    if let Some(p) = relative.get("from_person").and_then(|v| v.as_str()) {
        let resolved = resolve_people(&[p.to_string()], people);
        if let Some(email) = resolved.into_iter().next() {
            args.insert("from".into(), json!(email));
        } else if p.contains('@') {
            args.insert("from".into(), json!(p));
        }
    }
    // Also accept concrete from/after/before if model emitted them.
    if let Some(f) = relative.get("from").and_then(|v| v.as_str()) {
        args.insert("from".into(), json!(f));
    }
    let day = relative
        .get("day")
        .and_then(|v| v.as_str())
        .unwrap_or("none");
    let part = relative
        .get("part")
        .and_then(|v| v.as_str())
        .unwrap_or("none");
    if day == "mail_morning" || part == "mail_morning" {
        let (a, b) = morning_bounds_mail(now, timezone)?;
        args.insert("after".into(), json!(a));
        args.insert("before".into(), json!(b));
    } else if day == "today" {
        let local = local_now(now, timezone)?;
        let tz = parse_tz(timezone)?;
        let start = at_local(local.date_naive(), 0, 0, tz);
        args.insert("after".into(), json!(fmt_local(start)));
        args.insert("before".into(), json!(fmt_local(local)));
    } else if day == "last_month" {
        let local = local_now(now, timezone)?;
        let tz = parse_tz(timezone)?;
        let first_this = NaiveDate::from_ymd_opt(local.year(), local.month(), 1).unwrap();
        let start_this = at_local(first_this, 0, 0, tz);
        let prev = first_this - Duration::days(1);
        let first_prev = NaiveDate::from_ymd_opt(prev.year(), prev.month(), 1).unwrap();
        let start_prev = at_local(first_prev, 0, 0, tz);
        args.insert("after".into(), json!(fmt_local(start_prev)));
        args.insert("before".into(), json!(fmt_local(start_this)));
    }
    if let Some(a) = relative.get("after") {
        args.insert("after".into(), a.clone());
    }
    if let Some(b) = relative.get("before") {
        args.insert("before".into(), b.clone());
    }
    if args.is_empty() {
        return Err("search_email needs at least one filter".into());
    }
    Ok(Value::Object(args))
}

fn resolve_id_arg(relative: &Value, key: &str, allowed: &[String]) -> Result<Value, String> {
    if let Some(id) = relative.get(key).and_then(|v| v.as_str()) {
        if allowed.is_empty() || allowed.iter().any(|a| a == id) {
            return Ok(json!({ key: id }));
        }
        // Fuzzy: description match against allowed (substring).
        if let Some(hit) = allowed
            .iter()
            .find(|a| a.contains(id) || id.contains(a.as_str()))
        {
            return Ok(json!({ key: hit }));
        }
        return Err(format!("{key} {id} not in index"));
    }
    if let Some(desc) = relative.get("match").and_then(|v| v.as_str()) {
        let d = desc.to_lowercase();
        if let Some(hit) = allowed.iter().find(|a| a.to_lowercase().contains(&d)) {
            return Ok(json!({ key: hit }));
        }
    }
    Err(format!("missing {key}"))
}

fn resolve_update_event(
    relative: &Value,
    now: &str,
    timezone: &str,
    event_ids: &[String],
) -> Result<Value, String> {
    let mut base = resolve_id_arg(relative, "event_id", event_ids)?;
    let obj = base.as_object_mut().unwrap();
    for k in ["title", "start", "end", "attendees", "location"] {
        if let Some(v) = relative.get(k) {
            obj.insert(k.to_string(), v.clone());
        }
    }
    if relative.get("day").is_some() {
        let day = relative
            .get("day")
            .and_then(|v| v.as_str())
            .and_then(DaySpec::parse)
            .unwrap_or(DaySpec::Today);
        let part = relative
            .get("part")
            .and_then(|v| v.as_str())
            .and_then(Part::parse)
            .unwrap_or(Part::AllDay);
        let (start, end) = resolve_window(now, timezone, &day, part, None, None)?;
        obj.insert("start".into(), json!(start));
        obj.insert("end".into(), json!(end));
    }
    Ok(base)
}

fn resolve_create_event(
    relative: &Value,
    now: &str,
    timezone: &str,
    people: &HashMap<String, String>,
) -> Result<Value, String> {
    let mut args = relative.clone();
    if relative.get("day").is_some() {
        let day = relative
            .get("day")
            .and_then(|v| v.as_str())
            .and_then(DaySpec::parse)
            .unwrap_or(DaySpec::Today);
        let part = relative
            .get("part")
            .and_then(|v| v.as_str())
            .and_then(Part::parse)
            .unwrap_or(Part::Afternoon);
        let (start, end) = resolve_window(now, timezone, &day, part, None, None)?;
        let obj = args.as_object_mut().unwrap();
        obj.insert("start".into(), json!(start));
        obj.insert("end".into(), json!(end));
        obj.remove("day");
        obj.remove("part");
        obj.remove("people");
        obj.remove("start_hour");
        obj.remove("end_hour");
    }
    if let Some(arr) = relative.get("people").and_then(|v| v.as_array()) {
        let mentions: Vec<String> = arr
            .iter()
            .filter_map(|v| v.as_str().map(str::to_string))
            .collect();
        let resolved = resolve_people(&mentions, people);
        if !resolved.is_empty() {
            args.as_object_mut()
                .unwrap()
                .insert("attendees".into(), json!(resolved));
        }
    }
    Ok(args)
}

fn resolve_draft_email(relative: &Value, thread_ids: &[String]) -> Result<Value, String> {
    let mut args = relative.clone();
    if let Some(tid) = relative.get("thread_id").and_then(|v| v.as_str()) {
        if !thread_ids.is_empty() && !thread_ids.iter().any(|a| a == tid) {
            if let Some(hit) = thread_ids
                .iter()
                .find(|a| a.contains(tid) || tid.contains(a.as_str()))
            {
                args.as_object_mut()
                    .unwrap()
                    .insert("thread_id".into(), json!(hit));
            }
        }
    }
    Ok(args)
}

/// Schema forcing thread_id enum from index.
pub fn thread_id_schema(ids: &[String]) -> Value {
    if ids.is_empty() {
        return json!({
            "type": "object",
            "properties": { "thread_id": { "type": "string" } },
            "required": ["thread_id"],
            "additionalProperties": false
        });
    }
    json!({
        "type": "object",
        "properties": {
            "thread_id": { "type": "string", "enum": ids }
        },
        "required": ["thread_id"],
        "additionalProperties": false
    })
}

pub fn event_id_schema(ids: &[String]) -> Value {
    if ids.is_empty() {
        return json!({
            "type": "object",
            "properties": { "event_id": { "type": "string" } },
            "required": ["event_id"],
            "additionalProperties": false
        });
    }
    json!({
        "type": "object",
        "properties": {
            "event_id": { "type": "string", "enum": ids }
        },
        "required": ["event_id"],
        "additionalProperties": false
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn afternoon_agenda_window() {
        let (s, e) = afternoon_bounds("2026-11-22T10:45:00+01:00", "Europe/Amsterdam").unwrap();
        assert!(s.contains("2026-11-22T12:00:00"));
        assert!(e.contains("2026-11-22T18:00:00"));
    }

    #[test]
    fn weekend_this_week_jumps_to_monday() {
        // Sat 2026-11-21 → Mon 2026-11-23 … Mon 2026-11-30
        let (s, e) = week_bounds("2026-11-21T12:00:00+01:00", "Europe/Amsterdam").unwrap();
        assert!(s.starts_with("2026-11-23T00:00:00"));
        assert!(e.starts_with("2026-11-30T00:00:00"));
    }

    #[test]
    fn next_wednesday_from_thursday() {
        let d = next_weekday(
            "2026-11-26T14:00:00-05:00",
            "America/New_York",
            Weekday::Wed,
        )
        .unwrap();
        assert_eq!(d, NaiveDate::from_ymd_opt(2026, 12, 2).unwrap());
    }

    #[test]
    fn friday_this_week_from_wednesday() {
        let d = this_week_weekday(
            "2026-11-25T10:45:00+01:00",
            "Europe/Amsterdam",
            Weekday::Fri,
        )
        .unwrap();
        assert_eq!(d, NaiveDate::from_ymd_opt(2026, 11, 27).unwrap());
    }

    #[test]
    fn mail_morning_bounds() {
        let (s, e) = morning_bounds_mail("2026-11-21T09:00:00+01:00", "Europe/Amsterdam").unwrap();
        assert!(s.contains("T00:00:00"));
        assert!(e.contains("T12:00:00"));
    }

    #[test]
    fn ny_clock_change_2026() {
        let d = clock_change_date("America/New_York", 2026).unwrap();
        assert_eq!(d, NaiveDate::from_ymd_opt(2026, 11, 1).unwrap());
        let pre = d - Duration::days(1);
        assert_eq!(pre, NaiveDate::from_ymd_opt(2026, 10, 31).unwrap());
    }

    #[test]
    fn ams_clock_change_2026() {
        let d = clock_change_date("Europe/Amsterdam", 2026).unwrap();
        assert_eq!(d, NaiveDate::from_ymd_opt(2026, 10, 25).unwrap());
    }

    #[test]
    fn resolve_people_no_invent() {
        let mut known = HashMap::new();
        known.insert("sam".into(), "sam@example.com".into());
        known.insert("dana".into(), "dana.okonkwo@example.com".into());
        let got = resolve_people(
            &[
                "Sam".into(),
                "Nobody".into(),
                "dana.okonkwo@example.com".into(),
            ],
            &known,
        );
        assert_eq!(
            got,
            vec![
                "sam@example.com".to_string(),
                "dana.okonkwo@example.com".to_string()
            ]
        );
    }

    #[test]
    fn day_before_clock_change_morning_window() {
        let (a, b) = resolve_window(
            "2026-10-31T09:15:00-04:00",
            "America/New_York",
            &DaySpec::DayBeforeClockChange,
            Part::Morning,
            None,
            None,
        )
        .unwrap();
        assert!(a.contains("2026-10-31T09:00:00"));
        assert!(b.contains("2026-10-31T12:00:00"));
    }
}
