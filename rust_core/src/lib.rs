// 第一性原理：SMA 交叉是纯算术密集型，适合 Rust 向量化 + 零分配
// Python 侧仍保持 numpy/pandas 向量化，本模块作为热点预留，后续 PyO3 直连
use pyo3::prelude::*;
#[pyfunction]
fn sma_cross(prices: Vec<f64>, fast: usize, slow: usize) -> PyResult<Vec<String>> {
    if fast==0 || slow<=fast { return Err(pyo3::exceptions::PyValueError::new_err("fast<slow required")); }
    let n = prices.len();
    let mut signals = vec!["HOLD".to_string(); n];
    let mut sum_fast=0.0; let mut sum_slow=0.0;
    for i in 0..n {
        sum_fast += prices[i];
        sum_slow += prices[i];
        if i>=fast { sum_fast -= prices[i-fast]; }
        if i>=slow { sum_slow -= prices[i-slow]; }
        if i+1>=fast && i+1>=slow && i>0 {
            let fast_now = sum_fast/fast as f64;
            let slow_now = sum_slow/slow as f64;
            // 前一期
            let fast_prev = if i>=fast && i>=1 { (sum_fast - prices[i] + prices[i-fast])/fast as f64 } else { f64::NAN };
            let slow_prev = if i>=slow && i>=1 { (sum_slow - prices[i] + prices[i-slow])/slow as f64 } else { f64::NAN };
            if !fast_prev.is_nan() && !slow_prev.is_nan() {
                if fast_prev <= slow_prev && fast_now > slow_now { signals[i]="BUY".to_string(); }
                else if fast_prev >= slow_prev && fast_now < slow_now { signals[i]="SELL".to_string(); }
            }
        }
    }
    Ok(signals)
}
#[pymodule]
fn rust_core(_py: Python, m: &PyModule) -> PyResult<()> { m.add_function(wrap_pyfunction!(sma_cross,_py)?)?; Ok(()) }
