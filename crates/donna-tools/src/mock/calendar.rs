use chrono::{DateTime, Datelike, Duration, FixedOffset, NaiveDate, TimeZone, Timelike};
use chrono_tz::Tz;
use serde_json::Value;
use std::collections::{HashMap, HashSet};
use std::str::FromStr;

use crate::backend::{CalendarBackend, Event, FreeBusyBlock, FreeBusyMap};
use crate::mock::fixtures::load_calendar;
use crate::{Result, ToolError};
use donna_core::Untrusted;

const BYDAY: &[(&str, u32)] = &[
    ("MO", 0),
    ("TU", 1),
    ("WE", 2),
    ("TH", 3),
    ("FR", 4),
    ("SA", 5),
    ("SU", 6),
];

fn parse_byday(recurrence: &str) -> Option<HashSet<u32>> {
    let key = "BYDAY=";
    let start = recurrence.find(key)? + key.len();
    let end = recurrence[start..]
        .find(';')
        .map(|i| start + i)
        .unwrap_or(recurrence.len());
    let mut days = HashSet::new();
    for part in recurrence[start..end].split(',') {
        if let Some((_, v)) = BYDAY.iter().find(|(k, _)| *k == part) {
            days.insert(*v);
        }
    }
    Some(days)
}

#[derive(Debug, Clone)]
pub struct MockCalendar {
    data: Value,
    timezone: String,
}

impl MockCalendar {
    pub fn from_ref(ref_name: &str) -> Result<Self> {
        let data = load_calendar(ref_name)?;
        let timezone = data
            .get("timezone")
            .and_then(|t| t.as_str())
            .unwrap_or("UTC")
            .to_string();
        Ok(Self { data, timezone })
    }

    fn raw_events(&self) -> Result<&Vec<Value>> {
        self.data
            .get("events")
            .and_then(|e| e.as_array())
            .ok_or_else(|| ToolError::Other("calendar missing events".into()))
    }

    fn freebusy_raw(&self) -> HashMap<String, Vec<Value>> {
        let mut out = HashMap::new();
        let Some(fb) = self.data.get("freebusy").and_then(|f| f.as_object()) else {
            return out;
        };
        for (email, blocks) in fb {
            if let Some(arr) = blocks.as_array() {
                out.insert(email.clone(), arr.clone());
            }
        }
        out
    }
}

fn parse_tz(name: &str) -> Result<Tz> {
    Tz::from_str(name).map_err(|e| ToolError::Other(format!("bad timezone {name}: {e}")))
}

fn parse_dt(value: &str) -> Result<DateTime<FixedOffset>> {
    DateTime::parse_from_rfc3339(value)
        .map_err(|e| ToolError::Other(format!("bad datetime {value}: {e}")))
}

fn at_local(date: NaiveDate, hour: u32, minute: u32, tz: Tz) -> Result<DateTime<FixedOffset>> {
    let local = tz
        .with_ymd_and_hms(date.year(), date.month(), date.day(), hour, minute, 0)
        .single()
        .ok_or_else(|| {
            ToolError::Other(format!(
                "ambiguous/invalid local time {date} {hour:02}:{minute:02} in {tz}"
            ))
        })?;
    Ok(local.fixed_offset())
}

fn parse_event_dt(value: &str, tz: Tz) -> Result<DateTime<FixedOffset>> {
    if value.len() == 10
        && value.as_bytes().get(4) == Some(&b'-')
        && value.as_bytes().get(7) == Some(&b'-')
    {
        let d = NaiveDate::parse_from_str(value, "%Y-%m-%d")
            .map_err(|e| ToolError::Other(format!("bad date {value}: {e}")))?;
        return at_local(d, 0, 0, tz);
    }
    parse_dt(value)
}

fn weekday_num(d: NaiveDate) -> u32 {
    d.weekday().num_days_from_monday()
}

pub fn format_datetime(dt: &DateTime<FixedOffset>) -> String {
    dt.format("%Y-%m-%dT%H:%M:%S%:z").to_string()
}

fn event_from_raw(ev: &Value, start: &DateTime<FixedOffset>, end: &DateTime<FixedOffset>) -> Event {
    Event {
        event_id: ev
            .get("event_id")
            .and_then(|v| v.as_str())
            .unwrap_or("")
            .to_string(),
        title: Untrusted::new(
            ev.get("title")
                .and_then(|v| v.as_str())
                .unwrap_or("")
                .to_string(),
        ),
        start: format_datetime(start),
        end: format_datetime(end),
        attendees: ev
            .get("attendees")
            .and_then(|a| a.as_array())
            .map(|a| {
                a.iter()
                    .filter_map(|v| v.as_str().map(str::to_string))
                    .collect()
            })
            .unwrap_or_default(),
        recurrence: ev
            .get("recurrence")
            .and_then(|r| r.as_str())
            .map(str::to_string),
        all_day: ev.get("all_day").and_then(|a| a.as_bool()),
        location: ev
            .get("location")
            .and_then(|l| l.as_str())
            .map(str::to_string),
    }
}

/// Port of `_expand_event` in evals/golden/calendar/build.py.
pub fn expand_event(
    event: &Value,
    window_start: &DateTime<FixedOffset>,
    window_end: &DateTime<FixedOffset>,
    timezone: &str,
) -> Result<Vec<(DateTime<FixedOffset>, DateTime<FixedOffset>)>> {
    let tz = parse_tz(timezone)?;
    if event
        .get("all_day")
        .and_then(|a| a.as_bool())
        .unwrap_or(false)
    {
        let start_raw = event
            .get("start")
            .and_then(|s| s.as_str())
            .ok_or_else(|| ToolError::Other("all_day event missing start".into()))?;
        let start_d = NaiveDate::parse_from_str(&start_raw[..10], "%Y-%m-%d")
            .map_err(|e| ToolError::Other(format!("bad all_day start: {e}")))?;
        let last = if let Some(end_raw) = event.get("end").and_then(|e| e.as_str()) {
            let end_d = NaiveDate::parse_from_str(&end_raw[..10], "%Y-%m-%d")
                .map_err(|e| ToolError::Other(format!("bad all_day end: {e}")))?;
            if end_raw.len() == 10 {
                end_d - Duration::days(1)
            } else {
                end_d
            }
        } else {
            start_d
        };
        let mut out = Vec::new();
        let mut cur = start_d;
        while cur <= last {
            let s = at_local(cur, 0, 0, tz)?;
            let e = s + Duration::days(1);
            if e > *window_start && s < *window_end {
                out.push((s, e));
            }
            cur += Duration::days(1);
        }
        return Ok(out);
    }

    let start = parse_event_dt(
        event
            .get("start")
            .and_then(|s| s.as_str())
            .ok_or_else(|| ToolError::Other("event missing start".into()))?,
        tz,
    )?;
    let end = parse_event_dt(
        event
            .get("end")
            .and_then(|s| s.as_str())
            .ok_or_else(|| ToolError::Other("event missing end".into()))?,
        tz,
    )?;
    let recurrence = event
        .get("recurrence")
        .and_then(|r| r.as_str())
        .unwrap_or("");
    if recurrence.is_empty() {
        if end > *window_start && start < *window_end {
            return Ok(vec![(start, end)]);
        }
        return Ok(vec![]);
    }

    let days: HashSet<u32> = parse_byday(recurrence).unwrap_or_else(|| {
        let mut s = HashSet::new();
        s.insert(weekday_num(start.date_naive()));
        s
    });

    let duration = end - start;
    let mut cur_d = start.date_naive();
    let end_d = window_end.date_naive();
    let mut out = Vec::new();
    let mut guard = 0;
    while cur_d <= end_d && guard < 400 {
        if days.contains(&weekday_num(cur_d)) && cur_d >= start.date_naive() {
            let s = at_local(cur_d, start.hour(), start.minute(), tz)?;
            let e = s + duration;
            if e > *window_start && s < *window_end {
                out.push((s, e));
            }
        }
        cur_d += Duration::days(1);
        guard += 1;
    }
    Ok(out)
}

pub fn merge_busy(
    intervals: Vec<(DateTime<FixedOffset>, DateTime<FixedOffset>)>,
) -> Vec<(DateTime<FixedOffset>, DateTime<FixedOffset>)> {
    if intervals.is_empty() {
        return vec![];
    }
    let mut ordered = intervals;
    ordered.sort_by_key(|(s, _)| *s);
    let mut merged = vec![ordered[0]];
    for (s, e) in ordered.into_iter().skip(1) {
        let last = merged.last_mut().unwrap();
        if s <= last.1 {
            if e > last.1 {
                last.1 = e;
            }
        } else {
            merged.push((s, e));
        }
    }
    merged
}

/// Port of `earliest_slot` in build.py.
pub fn earliest_slot(
    duration_min: i64,
    after: DateTime<FixedOffset>,
    before: Option<DateTime<FixedOffset>>,
    busy: &[(DateTime<FixedOffset>, DateTime<FixedOffset>)],
) -> Option<(DateTime<FixedOffset>, DateTime<FixedOffset>)> {
    let before = before.unwrap_or_else(|| after + Duration::days(7));
    let dur = Duration::minutes(duration_min);
    if after + dur > before {
        return None;
    }
    let mut cursor = after;
    for &(bs, be) in busy {
        if be <= cursor {
            continue;
        }
        if bs >= cursor + dur {
            if cursor + dur <= before && cursor + dur <= bs {
                return Some((cursor, cursor + dur));
            }
            if bs >= before {
                break;
            }
        }
        if be > cursor {
            cursor = be;
        }
        if cursor + dur > before {
            return None;
        }
    }
    if cursor + dur <= before {
        return Some((cursor, cursor + dur));
    }
    None
}

impl CalendarBackend for MockCalendar {
    fn events(
        &self,
        range_start: &DateTime<FixedOffset>,
        range_end: &DateTime<FixedOffset>,
        timezone: &str,
    ) -> Result<Vec<Event>> {
        let mut out = Vec::new();
        for ev in self.raw_events()? {
            for (s, e) in expand_event(ev, range_start, range_end, timezone)? {
                out.push(event_from_raw(ev, &s, &e));
            }
        }
        out.sort_by(|a, b| a.start.cmp(&b.start).then(a.event_id.cmp(&b.event_id)));
        Ok(out)
    }

    fn freebusy(
        &self,
        attendees: &[String],
        range_start: &DateTime<FixedOffset>,
        range_end: &DateTime<FixedOffset>,
    ) -> Result<FreeBusyMap> {
        let fb = self.freebusy_raw();
        let mut out = FreeBusyMap::new();
        for email in attendees {
            let mut blocks = Vec::new();
            for block in fb.get(email).into_iter().flatten() {
                let s = parse_dt(block.get("start").and_then(|v| v.as_str()).unwrap_or(""))?;
                let e = parse_dt(block.get("end").and_then(|v| v.as_str()).unwrap_or(""))?;
                if e > *range_start && s < *range_end {
                    blocks.push(FreeBusyBlock {
                        start: format_datetime(&s),
                        end: format_datetime(&e),
                    });
                }
            }
            out.insert(email.clone(), blocks);
        }
        Ok(out)
    }

    fn event_count(&self) -> usize {
        self.raw_events().map(|e| e.len()).unwrap_or(0)
    }

    fn timezone(&self) -> &str {
        &self.timezone
    }

    fn owner_busy(
        &self,
        after: &DateTime<FixedOffset>,
        before: &DateTime<FixedOffset>,
        timezone: &str,
    ) -> Result<Vec<(DateTime<FixedOffset>, DateTime<FixedOffset>)>> {
        let mut intervals = Vec::new();
        for ev in self.raw_events()? {
            intervals.extend(expand_event(ev, after, before, timezone)?);
        }
        Ok(merge_busy(intervals))
    }
}

pub fn attendee_busy(
    cal: &MockCalendar,
    emails: &[String],
    after: &DateTime<FixedOffset>,
    before: &DateTime<FixedOffset>,
) -> Result<Vec<(DateTime<FixedOffset>, DateTime<FixedOffset>)>> {
    let fb = cal.freebusy_raw();
    let mut intervals = Vec::new();
    for email in emails {
        for block in fb.get(email).into_iter().flatten() {
            let s = parse_dt(block.get("start").and_then(|v| v.as_str()).unwrap_or(""))?;
            let e = parse_dt(block.get("end").and_then(|v| v.as_str()).unwrap_or(""))?;
            if e > *after && s < *before {
                intervals.push((s, e));
            }
        }
    }
    Ok(merge_busy(intervals))
}

pub fn find_free_slot(
    cal: &MockCalendar,
    duration_min: i64,
    after: DateTime<FixedOffset>,
    before: Option<DateTime<FixedOffset>>,
    timezone: &str,
    attendees: &[String],
) -> Result<Option<(DateTime<FixedOffset>, DateTime<FixedOffset>)>> {
    let end_bound = before.unwrap_or_else(|| after + Duration::days(7));
    let mut busy = cal.owner_busy(&after, &end_bound, timezone)?;
    if !attendees.is_empty() {
        let att = attendee_busy(cal, attendees, &after, &end_bound)?;
        busy = merge_busy(busy.into_iter().chain(att).collect());
    }
    Ok(earliest_slot(duration_min, after, before, &busy))
}
