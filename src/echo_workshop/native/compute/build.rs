// 将唯一产品版本和当前 Rust 源码摘要嵌入计算程序。
use sha2::{Digest, Sha256};
use std::path::PathBuf;

fn main() {
    let root = PathBuf::from(std::env::var_os("CARGO_MANIFEST_DIR").unwrap());
    let version_file = root.join("../../__init__.py");
    let version_source = std::fs::read_to_string(&version_file).expect("product version");
    let line = version_source
        .lines()
        .find(|line| line.starts_with("__version__ = "))
        .expect("unique version assignment");
    let version = line
        .split_once('=')
        .unwrap()
        .1
        .trim()
        .trim_matches(['\'', '"']);
    println!("cargo:rerun-if-changed={}", version_file.display());
    println!("cargo:rustc-env=ECHO_PRODUCT_VERSION={version}");
    let mut members: Vec<String> = [
        "Cargo.toml",
        "Cargo.lock",
        "rust-toolchain.toml",
        "rustfmt.toml",
        "clippy.toml",
        "build.rs",
        "data/constants.json",
    ]
    .iter()
    .map(|s| (*s).into())
    .collect();
    for item in std::fs::read_dir(root.join("src")).expect("Rust sources") {
        let file = item.expect("source entry").path();
        if file.extension().and_then(|e| e.to_str()) == Some("rs") {
            members.push(format!(
                "src/{}",
                file.file_name().unwrap().to_str().unwrap()
            ));
        }
    }
    members.sort();
    let mut hash = Sha256::new();
    for member in members {
        println!("cargo:rerun-if-changed={member}");
        hash.update(member.as_bytes());
        hash.update([0]);
        hash.update(std::fs::read(root.join(&member)).expect("source member"));
        hash.update([0]);
    }
    println!("cargo:rustc-env=ECHO_SOURCE_ID={:x}", hash.finalize());
}
