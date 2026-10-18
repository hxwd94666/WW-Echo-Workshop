// 有界保留显式库存快照与原生可用性证据，失效仅重传输入，不替换计算后端。
use crate::domain::{Record, uuid};
use crate::{ComputeError, ComputeResult, checkpoint};
use serde::Deserialize;
use std::collections::VecDeque;
use std::mem::size_of;
use std::sync::{Arc, Mutex, OnceLock, atomic::AtomicBool};

const CACHE_BYTES: usize = 8 * 1024 * 1024;
const CACHE_ENTRIES: usize = 2;

#[derive(Deserialize, Default)]
pub struct SearchText {
    pub literal: String,
    pub plain: String,
    pub keys: String,
}

#[derive(Deserialize)]
pub struct Row {
    pub record: Record,
    pub kind: String,
    pub captured_us: Option<String>,
    #[serde(default)]
    pub search: SearchText,
    #[serde(default)]
    pub reservation: String,
}

pub struct Snapshot {
    pub rows: Vec<Row>,
    pub usable: Vec<bool>,
    pub needs_correction: Vec<bool>,
    pub issues: Vec<Vec<String>>,
    bytes: usize,
}

#[derive(Default)]
struct Cache {
    entries: VecDeque<(String, Arc<Snapshot>)>,
    bytes: usize,
}

fn cache() -> &'static Mutex<Cache> {
    static CACHE: OnceLock<Mutex<Cache>> = OnceLock::new();
    CACHE.get_or_init(|| Mutex::new(Cache::default()))
}

fn row_heap_bytes(row: &Row) -> usize {
    let record = &row.record;
    let mut bytes = 0;
    for value in [
        &record.item_id,
        &record.profile_id,
        &record.name,
        &record.sonata,
        &record.review,
        &record.owner,
        &record.owner_state,
        &record.note,
        &record.captured_at,
        &row.kind,
        &row.reservation,
        &row.search.literal,
        &row.search.plain,
        &row.search.keys,
    ] {
        bytes += value.capacity();
    }
    bytes += row.captured_us.as_ref().map_or(0, String::capacity);
    bytes += record.issues.capacity() * size_of::<String>();
    bytes += record.issues.iter().map(String::capacity).sum::<usize>();
    for stats in [&record.main_stats, &record.sub_stats] {
        bytes += stats.capacity() * size_of::<crate::scoring::Stat>();
        bytes += stats.iter().map(|s| s.stat_name.capacity()).sum::<usize>();
    }
    bytes
}

pub fn resolve(
    identity: &str,
    rows: Option<Vec<Row>>,
    cancel: &AtomicBool,
) -> ComputeResult<(Arc<Snapshot>, bool)> {
    checkpoint(cancel)?;
    if !uuid(identity) {
        return Err(ComputeError::new("invalid_request", "库存快照身份错误"));
    }
    {
        let mut cache = cache().lock().unwrap();
        if let Some(index) = cache.entries.iter().position(|(key, _)| key == identity) {
            if rows.is_some() {
                return Err(ComputeError::new(
                    "invalid_request",
                    "库存快照身份禁止重定义",
                ));
            }
            let entry = cache.entries.remove(index).unwrap();
            let snapshot = Arc::clone(&entry.1);
            cache.entries.push_back(entry);
            return Ok((snapshot, true));
        }
    }
    let rows =
        rows.ok_or_else(|| ComputeError::new("cache_miss", "库存快照已失效，需重传明确输入"))?;
    if rows.len() > 20000 {
        return Err(ComputeError::new("input_limit", "库存快照超过单批规模限额"));
    }
    let mut usable = Vec::with_capacity(rows.len());
    let mut needs_correction = Vec::with_capacity(rows.len());
    let mut issues = Vec::with_capacity(rows.len());
    let mut bytes = size_of::<Snapshot>() + identity.len() + rows.capacity() * size_of::<Row>();
    for row in &rows {
        checkpoint(cancel)?;
        row.record.validate_identity()?;
        let problems = row.record.validation_issues();
        let invalid = !problems.is_empty();
        bytes += problems.capacity() * size_of::<String>()
            + problems.iter().map(String::capacity).sum::<usize>();
        issues.push(problems);
        usable.push(row.record.review == "confirmed" && !invalid);
        needs_correction.push(invalid);
        bytes += row_heap_bytes(row);
    }
    bytes += usable.capacity()
        + needs_correction.capacity()
        + issues.capacity() * size_of::<Vec<String>>();
    let snapshot = Arc::new(Snapshot {
        rows,
        usable,
        needs_correction,
        issues,
        bytes,
    });
    checkpoint(cancel)?;
    let retained = bytes <= CACHE_BYTES;
    if retained {
        let mut cache = cache().lock().unwrap();
        while cache.entries.len() >= CACHE_ENTRIES || cache.bytes + bytes > CACHE_BYTES {
            let (_, previous) = cache.entries.pop_front().unwrap();
            cache.bytes -= previous.bytes;
        }
        cache.bytes += bytes;
        cache
            .entries
            .push_back((identity.to_owned(), Arc::clone(&snapshot)));
    }
    Ok((snapshot, retained))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn immutable_ids_missing_entries_and_heap_accounting_are_explicit() {
        let id = "33333333-3333-4333-8333-333333333333";
        let cancel = AtomicBool::new(false);
        let (first, retained) = resolve(id, Some(Vec::new()), &cancel).unwrap();
        assert!(retained);
        assert!(Arc::ptr_eq(&first, &resolve(id, None, &cancel).unwrap().0));
        assert_eq!(
            resolve(id, Some(Vec::new()), &cancel).err().unwrap().kind,
            "invalid_request"
        );
        assert_eq!(
            resolve("44444444-4444-4444-8444-444444444444", None, &cancel)
                .err()
                .unwrap()
                .kind,
            "cache_miss"
        );
        let cache = cache().lock().unwrap();
        assert!(cache.entries.len() <= CACHE_ENTRIES && cache.bytes <= CACHE_BYTES);
    }
}
