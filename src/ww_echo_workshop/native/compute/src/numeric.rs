// 复现评分逐项截断、十进制评级阈值及稳定的标准身份文本。
use crate::{ComputeError, ComputeResult};

pub const ENTRY_CENTS_LIMIT: i64 = 100_000_000_000_000;
pub const ECHO_CENTS_LIMIT: i64 = ENTRY_CENTS_LIMIT * 7;
pub const ROLE_CENTS_LIMIT: i64 = ECHO_CENTS_LIMIT * 5;
pub const TARGET_CENTS_LIMIT: i64 = 100_000_000_000_000;

pub fn entry_cents(value: f64, weight: f64, maximum: f64) -> ComputeResult<i64> {
    checked_entry(value, weight, maximum, false)
}

pub(crate) fn observed_entry_cents(value: f64, weight: f64, maximum: f64) -> ComputeResult<i64> {
    checked_entry(value, weight, maximum, true)
}

fn checked_entry(value: f64, weight: f64, maximum: f64, observation: bool) -> ComputeResult<i64> {
    if !value.is_finite()
        || !weight.is_finite()
        || !maximum.is_finite()
        || (!observation && value < 0.0)
        || weight < 0.0
        || maximum <= 0.0
    {
        return Err(ComputeError::new(
            "invalid_numeric",
            "评分输入须为有限非负数",
        ));
    }
    // 与当前合同保持乘、除、乘和 epsilon 后的逐词条截断顺序。
    let scaled = ((value * weight / maximum * 50.0) + 1e-12) * 100.0;
    if !scaled.is_finite() || scaled.abs() > ENTRY_CENTS_LIMIT as f64 {
        return Err(ComputeError::new("numeric_limit", "词条分数超过计算限额"));
    }
    Ok(scaled.trunc() as i64)
}

pub fn python_float(value: f64) -> String {
    let raw = serde_json::to_string(&value).expect("finite float");
    let (mantissa, supplied_exponent) = match raw.split_once(['e', 'E']) {
        Some((m, e)) => (m, e.parse::<i32>().expect("float exponent")),
        None => (raw.as_str(), 0),
    };
    let sign = if mantissa.starts_with('-') { "-" } else { "" };
    let mantissa = mantissa.trim_start_matches('-');
    let decimal = mantissa.find('.').unwrap_or(mantissa.len()) as i32;
    let digits = mantissa.replace('.', "");
    let Some(first) = digits.bytes().position(|v| v != b'0') else {
        return format!("{sign}0.0");
    };
    let clean = digits[first..].trim_end_matches('0');
    let exponent = decimal - first as i32 - 1 + supplied_exponent;
    if (-4..16).contains(&exponent) {
        let position = exponent + 1;
        let body = if position <= 0 {
            format!("0.{}{}", "0".repeat((-position) as usize), clean)
        } else if position as usize >= clean.len() {
            format!("{}{}.0", clean, "0".repeat(position as usize - clean.len()))
        } else {
            let p = position as usize;
            format!("{}.{}", &clean[..p], &clean[p..])
        };
        return format!("{sign}{body}");
    }
    let m = if clean.len() == 1 {
        clean.to_owned()
    } else {
        format!("{}.{}", &clean[..1], &clean[1..])
    };
    format!(
        "{sign}{m}e{}{:02}",
        if exponent < 0 { "-" } else { "+" },
        exponent.abs()
    )
}

pub fn decimal_scale(value: f64, scale: u32) -> ComputeResult<f64> {
    if !value.is_finite() || !(0.0..=1.0).contains(&value) {
        return Err(ComputeError::new("invalid_grade", "评级比例须位于 0–1"));
    }
    if value == 0.0 {
        return Ok(value);
    }
    let raw = python_float(value);
    let (mantissa, exponent) = match raw.split_once('e') {
        Some((m, e)) => (
            m,
            e.parse::<i32>()
                .map_err(|_| ComputeError::new("invalid_grade", "评级指数错误"))?,
        ),
        None => (raw.as_str(), 0),
    };
    let decimals = mantissa.split_once('.').map_or(0, |(_, d)| d.len() as i32);
    let digits = mantissa
        .replace('.', "")
        .parse::<u128>()
        .map_err(|_| ComputeError::new("invalid_grade", "评级精度错误"))?;
    let product = digits
        .checked_mul(u128::from(scale))
        .ok_or_else(|| ComputeError::new("numeric_limit", "评级数值溢出"))?;
    format!("{product}e{}", exponent - decimals)
        .parse()
        .map_err(|_| ComputeError::new("invalid_grade", "评级阈值错误"))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn decimal_boundary_and_float_identity() {
        assert_eq!(decimal_scale(0.56, 50).unwrap(), 28.0);
        assert_eq!(python_float(1e-6), "1e-06");
        assert_eq!(python_float(1e-4), "0.0001");
        assert_eq!(python_float(0.0), "0.0");
        assert_eq!(python_float(1e20), "1e+20");
    }
}
