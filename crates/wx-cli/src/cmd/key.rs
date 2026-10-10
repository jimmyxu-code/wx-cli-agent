use std::time::Duration;

#[cfg(target_os = "macos")]
mod trial_guard {
    use serde_json::Value;
    use std::{
        fs,
        io::Write,
        os::unix::fs::{MetadataExt, OpenOptionsExt, PermissionsExt},
        path::{Path, PathBuf},
        process::{Command, Stdio},
    };
    type SafeResult<T> = Result<T, Box<dyn std::error::Error>>;

    fn command(program: &str, args: &[&std::ffi::OsStr]) -> SafeResult<Vec<u8>> {
        let out = Command::new(program)
            .args(args)
            .output()
            .map_err(|_| "trial_metadata_command_failed")?;
        if !out.status.success() {
            return Err("trial_metadata_command_failed".into());
        }
        Ok(out.stdout)
    }
    fn plist_json(bytes: &[u8]) -> SafeResult<Value> {
        let mut child = Command::new("/usr/bin/plutil")
            .args(["-convert", "json", "-o", "-", "--", "-"])
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .spawn()
            .map_err(|_| "trial_plist_conversion_failed")?;
        child
            .stdin
            .take()
            .ok_or("trial_plist_conversion_failed")?
            .write_all(bytes)
            .map_err(|_| "trial_plist_conversion_failed")?;
        let output = child
            .wait_with_output()
            .map_err(|_| "trial_plist_conversion_failed")?;
        if !output.status.success() {
            return Err("trial_plist_conversion_failed".into());
        }
        serde_json::from_slice(&output.stdout).map_err(|_| "trial_plist_invalid".into())
    }
    pub fn digest(path: &Path) -> SafeResult<String> {
        let raw = command(
            "/usr/bin/shasum",
            &["-a".as_ref(), "256".as_ref(), path.as_os_str()],
        )?;
        let hash = std::str::from_utf8(&raw)
            .ok()
            .and_then(|s| s.split_whitespace().next())
            .ok_or("trial_digest_failed")?;
        if hash.len() != 64 || !hash.bytes().all(|b| b.is_ascii_hexdigit()) {
            return Err("trial_digest_failed".into());
        }
        Ok(hash.to_ascii_lowercase())
    }
    pub(super) fn private_owned(path: &Path, uid: u32, directory: bool) -> SafeResult<()> {
        let meta = fs::symlink_metadata(path).map_err(|_| "trial_private_metadata_missing")?;
        if meta.file_type().is_symlink()
            || meta.uid() != uid
            || (meta.mode() & 0o077 != 0)
            || (directory && !meta.is_dir())
            || (!directory && !meta.is_file())
        {
            return Err("trial_private_metadata_required".into());
        }
        Ok(())
    }
    fn read_marker(root: &Path, name: &str, uid: u32) -> SafeResult<Value> {
        let file = root.join(name);
        private_owned(&file, uid, false)?;
        let data = fs::read(file).map_err(|_| "trial_marker_unreadable")?;
        if data.len() > 1024 * 1024 {
            return Err("trial_marker_too_large".into());
        }
        serde_json::from_slice(&data).map_err(|_| "trial_marker_invalid".into())
    }
    fn validate_ticket(
        ticket: &Value,
        pid: u32,
        trial_digest: &str,
        original_digest: &str,
    ) -> SafeResult<()> {
        if ticket.get("pid").and_then(Value::as_u64) != Some(pid as u64)
            || ticket.get("trialExecutableSha256").and_then(Value::as_str) != Some(trial_digest)
            || ticket
                .get("originalExecutableSha256")
                .and_then(Value::as_str)
                != Some(original_digest)
        {
            return Err("trial_ticket_mismatch".into());
        }
        Ok(())
    }
    #[link(name = "proc")]
    unsafe extern "C" {
        fn proc_pidpath(pid: i32, buffer: *mut libc::c_void, buffersize: u32) -> i32;
    }

    fn process_path(pid: u32) -> SafeResult<PathBuf> {
        if pid == 0 || pid > i32::MAX as u32 {
            return Err("trial_pid_invalid".into());
        }
        let mut buffer = [0u8; 4096];
        let n =
            unsafe { proc_pidpath(pid as i32, buffer.as_mut_ptr().cast(), buffer.len() as u32) };
        if n <= 0 {
            return Err("trial_pid_path_unavailable".into());
        }
        let path = std::str::from_utf8(buffer.split(|x| *x == 0).next().unwrap_or(&[]))
            .map_err(|_| "trial_pid_path_invalid")?;
        PathBuf::from(path)
            .canonicalize()
            .map_err(|_| "trial_pid_path_invalid".into())
    }
    pub fn validate(pid: u32, app: &Path) -> SafeResult<String> {
        let uid = unsafe { libc::geteuid() };
        if uid == 0 || unsafe { libc::getuid() } != uid {
            return Err("trial_same_user_required".into());
        }
        if app.file_name().and_then(|n| n.to_str()) != Some("WeChat-trial.app") {
            return Err("trial_app_name_required".into());
        }
        let meta = fs::symlink_metadata(app).map_err(|_| "trial_app_missing")?;
        if meta.file_type().is_symlink() || meta.uid() != uid {
            return Err("trial_app_owner_invalid".into());
        }
        let app = app.canonicalize().map_err(|_| "trial_app_missing")?;
        if app.starts_with("/Applications") || app.starts_with("/System") {
            return Err("trial_original_app_forbidden".into());
        }
        let root = app.parent().ok_or("trial_private_root_missing")?;
        private_owned(root, uid, true)?;
        let plan = read_marker(root, "signing-plan.json", uid)?;
        let integrity = read_marker(root, "original-integrity.json", uid)?;
        let ticket = read_marker(root, "scan-trial.json", uid)?;
        let exe = app
            .join("Contents/MacOS/WeChat")
            .canonicalize()
            .map_err(|_| "trial_executable_missing")?;
        if exe != app.join("Contents/MacOS/WeChat") || process_path(pid)? != exe {
            return Err("trial_pid_executable_mismatch".into());
        }
        let pid_text = pid.to_string();
        let uid_raw = command(
            "/bin/ps",
            &[
                "-p".as_ref(),
                pid_text.as_ref(),
                "-o".as_ref(),
                "uid=".as_ref(),
            ],
        )?;
        if std::str::from_utf8(&uid_raw)
            .ok()
            .and_then(|s| s.trim().parse::<u32>().ok())
            != Some(uid)
        {
            return Err("trial_process_owner_mismatch".into());
        }
        let original = Path::new("/Applications/WeChat.app/Contents/MacOS/WeChat");
        let original_hash = digest(original)?;
        if integrity
            .get("originalExecutableSha256")
            .and_then(Value::as_str)
            != Some(original_hash.as_str())
        {
            return Err("trial_original_integrity_mismatch".into());
        }
        validate_ticket(&ticket, pid, &digest(&exe)?, &original_hash)?;
        command(
            "/usr/bin/codesign",
            &[
                "--verify".as_ref(),
                "--deep".as_ref(),
                "--strict".as_ref(),
                app.as_os_str(),
            ],
        )?;
        let info = plist_json(
            &fs::read(app.join("Contents/Info.plist")).map_err(|_| "trial_info_missing")?,
        )?;
        if info.get("CFBundleIdentifier").and_then(Value::as_str) != Some("com.tencent.xinWeChat")
            || info.get("CFBundleExecutable").and_then(Value::as_str) != Some("WeChat")
        {
            return Err("trial_bundle_identity_mismatch".into());
        }
        let ent_raw = command(
            "/usr/bin/codesign",
            &[
                "-d".as_ref(),
                "--entitlements".as_ref(),
                ":-".as_ref(),
                app.as_os_str(),
            ],
        )?;
        let actual = plist_json(&ent_raw)?;
        let main = plan
            .get("signingOrder")
            .and_then(Value::as_array)
            .and_then(|items| {
                items
                    .iter()
                    .find(|i| i.get("kind").and_then(Value::as_str) == Some("main"))
            })
            .ok_or("trial_signing_plan_main_missing")?;
        if main.get("target").and_then(Value::as_str) != Some(".")
            || main.get("binary").and_then(Value::as_str) != Some("Contents/MacOS/WeChat")
            || main.get("entitlements") != Some(&actual)
            || actual
                .get("com.apple.security.get-task-allow")
                .and_then(Value::as_bool)
                != Some(true)
        {
            return Err("trial_debug_entitlements_mismatch".into());
        }
        let sip = command("/usr/bin/csrutil", &["status".as_ref()])?;
        if !String::from_utf8_lossy(&sip)
            .to_lowercase()
            .contains("status: enabled")
        {
            return Err("trial_requires_sip_enabled".into());
        }
        // Re-check the process after metadata checks before any memory access.
        if process_path(pid)? != exe {
            return Err("trial_pid_changed".into());
        }
        info.get("CFBundleShortVersionString")
            .and_then(Value::as_str)
            .map(str::to_owned)
            .ok_or_else(|| "trial_version_missing".into())
    }
    pub fn save_private(store: &wx_keychain::KeyStore) -> SafeResult<()> {
        let path = wx_keychain::KeyStore::default_path().map_err(|_| "trial_store_path_failed")?;
        let parent = path.parent().ok_or("trial_store_path_failed")?;
        fs::create_dir_all(parent).map_err(|_| "trial_store_directory_failed")?;
        let uid = unsafe { libc::geteuid() };
        let meta = fs::symlink_metadata(parent).map_err(|_| "trial_store_directory_failed")?;
        if meta.file_type().is_symlink() || meta.uid() != uid {
            return Err("trial_store_owner_invalid".into());
        }
        fs::set_permissions(parent, fs::Permissions::from_mode(0o700))
            .map_err(|_| "trial_store_permissions_failed")?;
        if path.exists() {
            private_owned(&path, uid, false)?;
        }
        let temp = parent.join(format!(".scan-trial-{}.tmp", std::process::id()));
        let mut file = fs::OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&temp)
            .map_err(|_| "trial_store_private_temp_failed")?;
        let result = (|| -> SafeResult<()> {
            let text =
                toml::to_string_pretty(store).map_err(|_| "trial_store_serialization_failed")?;
            file.write_all(text.as_bytes())
                .map_err(|_| "trial_store_write_failed")?;
            file.sync_all().map_err(|_| "trial_store_write_failed")?;
            fs::rename(&temp, path).map_err(|_| "trial_store_commit_failed")?;
            Ok(())
        })();
        if result.is_err() {
            let _ = fs::remove_file(temp);
        }
        result
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        #[test]
        fn ticket_binds_pid_and_both_executable_digests() {
            let valid = serde_json::json!({"pid":42,"trialExecutableSha256":"trial","originalExecutableSha256":"original"});
            assert!(validate_ticket(&valid, 42, "trial", "original").is_ok());
            assert!(validate_ticket(&valid, 43, "trial", "original").is_err());
            assert!(validate_ticket(&valid, 42, "changed", "original").is_err());
            assert!(validate_ticket(&valid, 42, "trial", "changed").is_err());
        }
        #[test]
        fn markers_reject_symlinks_and_world_readable_files() {
            let root = tempfile::tempdir().unwrap();
            let file = root.path().join("marker");
            fs::write(&file, b"{}").unwrap();
            fs::set_permissions(&file, fs::Permissions::from_mode(0o644)).unwrap();
            let uid = unsafe { libc::geteuid() };
            assert!(private_owned(&file, uid, false).is_err());
            fs::set_permissions(&file, fs::Permissions::from_mode(0o600)).unwrap();
            assert!(private_owned(&file, uid, false).is_ok());
            let link = root.path().join("link");
            std::os::unix::fs::symlink(&file, &link).unwrap();
            assert!(private_owned(&link, uid, false).is_err());
        }
        #[test]
        fn disabled_or_original_application_names_are_rejected_before_attach() {
            assert!(validate(42, Path::new("/Applications/WeChat.app")).is_err());
            assert!(validate(42, Path::new("/tmp/WeChat-trial.disabled")).is_err());
        }
    }
}

#[cfg(target_os = "macos")]
mod capture_import {
    use serde_json::Value;
    use std::{
        fs,
        io::Read,
        path::{Component, Path, PathBuf},
    };
    type SafeResult<T> = Result<T, Box<dyn std::error::Error>>;

    fn scoped_database(root: &Path, relative: &str) -> SafeResult<PathBuf> {
        let rel = Path::new(relative);
        if relative.is_empty()
            || relative.contains('\\')
            || relative.contains('\0')
            || !rel.components().all(|c| matches!(c, Component::Normal(_)))
            || rel.extension().and_then(|e| e.to_str()) != Some("db")
        {
            return Err("capture_database_path_invalid".into());
        }
        let resolved = root
            .join(rel)
            .canonicalize()
            .map_err(|_| "capture_database_missing")?;
        if !resolved.starts_with(root) || !resolved.is_file() {
            return Err("capture_database_outside_account".into());
        }
        let uid = unsafe { libc::geteuid() };
        use std::os::unix::fs::MetadataExt;
        if fs::metadata(&resolved)
            .map_err(|_| "capture_database_metadata_failed")?
            .uid()
            != uid
        {
            return Err("capture_database_owner_mismatch".into());
        }
        Ok(resolved)
    }
    fn validate_pair(path: &Path, encoded: &str) -> SafeResult<wx_decrypt::EncKeyPair> {
        let mut key = [0u8; 32];
        if encoded.len() != 64 || hex::decode_to_slice(encoded, &mut key).is_err() {
            return Err("capture_key_format_invalid".into());
        }
        let salt = wx_decrypt::read_db_salt(path).map_err(|_| "capture_database_salt_failed")?;
        let params = &wx_decrypt::MACOS_4_1_7_31;
        let mut first = vec![0u8; params.page_size];
        fs::File::open(path)
            .and_then(|mut file| file.read_exact(&mut first))
            .map_err(|_| "capture_database_header_failed")?;
        if !wx_decrypt::validate_enc_key(&first, &key, &salt, params) {
            return Err("capture_database_hmac_failed".into());
        }
        Ok(wx_decrypt::EncKeyPair { key, salt })
    }
    fn merge_pairs(
        entry: Option<&wx_keychain::AccountKey>,
        imported: &[wx_decrypt::EncKeyPair],
    ) -> SafeResult<Vec<wx_decrypt::EncKeyPair>> {
        let mut by_salt = std::collections::BTreeMap::new();
        let mut insert_encoded = |key: &str, salt: &str| -> SafeResult<()> {
            let mut k = [0u8; 32];
            let mut s = [0u8; 16];
            if hex::decode_to_slice(key, &mut k).is_err()
                || hex::decode_to_slice(salt, &mut s).is_err()
            {
                return Err("capture_existing_store_pair_invalid".into());
            }
            by_salt.insert(s, k);
            Ok(())
        };
        if let Some(entry) = entry {
            if let (Some(key), Some(salt)) = (&entry.enc_key, &entry.enc_key_salt) {
                insert_encoded(key, salt)?;
            }
            for pair in &entry.enc_keys {
                insert_encoded(&pair.enc_key, &pair.salt)?;
            }
        }
        // Only a newly HMAC-validated value can replace the old value for this salt.
        for pair in imported {
            by_salt.insert(pair.salt, pair.key);
        }
        Ok(by_salt
            .into_iter()
            .map(|(salt, key)| wx_decrypt::EncKeyPair { key, salt })
            .collect())
    }
    pub fn run(capture: &Path, db_root: &Path) -> SafeResult<()> {
        let uid = unsafe { libc::geteuid() };
        if uid == 0 || unsafe { libc::getuid() } != uid {
            return Err("capture_same_user_required".into());
        }
        super::trial_guard::private_owned(capture, uid, false)?;
        let metadata = fs::metadata(capture).map_err(|_| "capture_file_unreadable")?;
        if metadata.len() > 4 * 1024 * 1024 {
            return Err("capture_file_too_large".into());
        }
        let root = db_root
            .canonicalize()
            .map_err(|_| "capture_database_root_invalid")?;
        let accounts =
            wx_keychain::find_account_dirs().map_err(|_| "capture_account_discovery_failed")?;
        let candidates: Vec<_> = accounts
            .iter()
            .filter(|account| {
                account
                    .data_dir
                    .join("db_storage")
                    .canonicalize()
                    .ok()
                    .as_ref()
                    == Some(&root)
            })
            .collect();
        if candidates.len() != 1 {
            return Err("capture_database_root_not_one_local_account".into());
        }
        let account = candidates[0];
        let document: Value =
            serde_json::from_slice(&fs::read(capture).map_err(|_| "capture_file_unreadable")?)
                .map_err(|_| "capture_document_invalid")?;
        let keys = document
            .get("keys")
            .and_then(Value::as_object)
            .ok_or("capture_document_keys_missing")?;
        if keys.is_empty() || keys.len() > 4096 {
            return Err("capture_document_keys_count_invalid".into());
        }
        let mut imported = Vec::new();
        // Finish every validation before touching the persistent key store.
        for (relative, record) in keys {
            let path = scoped_database(&root, relative)?;
            let encoded = record
                .get("enc_key")
                .and_then(Value::as_str)
                .ok_or("capture_document_key_missing")?;
            imported.push(validate_pair(&path, encoded)?);
        }
        let mut store =
            wx_keychain::KeyStore::load_default().map_err(|_| "capture_store_load_failed")?;
        let merged = merge_pairs(store.get(&account.account_id), &imported)?;
        store.set_enc_keys(
            &account.account_id,
            &merged,
            "validated-capture",
            None,
            Some(account.base_wxid.clone()),
        );
        super::trial_guard::save_private(&store)?;
        println!(
            "{}",
            serde_json::json!({"importedValidatedPairs":imported.len(),"matchedAccounts":1,"savedPrivately":true})
        );
        Ok(())
    }
    #[cfg(test)]
    mod tests {
        use super::*;
        fn artificial_db(root: &Path) -> PathBuf {
            let path = root.join("fixture.db");
            let mut page = vec![0u8; 4096];
            page[..16].fill(9);
            page[4032..].copy_from_slice(&hex::decode("1da082b48452348efe17c886460c32e8805bafb69280481d05ebb832bf799127ddbf4a83508c00272d1c778a0d6bf24942236916df60689f4abaa0eff0a17703").unwrap());
            fs::write(&path, page).unwrap();
            path
        }
        #[test]
        fn import_requires_real_first_page_hmac() {
            let root = tempfile::tempdir().unwrap();
            let path = artificial_db(root.path());
            assert!(validate_pair(&path, &hex::encode([7u8; 32])).is_ok());
            assert!(validate_pair(&path, &hex::encode([8u8; 32])).is_err());
            let mut bytes = fs::read(&path).unwrap();
            bytes[50] ^= 1;
            fs::write(&path, bytes).unwrap();
            assert!(validate_pair(&path, &hex::encode([7u8; 32])).is_err());
        }
        #[test]
        fn import_rejects_traversal_and_external_symlinks() {
            let root = tempfile::tempdir().unwrap();
            let outer = tempfile::tempdir().unwrap();
            let outside = artificial_db(outer.path());
            std::os::unix::fs::symlink(&outside, root.path().join("link.db")).unwrap();
            for name in [
                "../fixture.db",
                "/tmp/fixture.db",
                "link.db",
                "a\\fixture.db",
            ] {
                assert!(scoped_database(root.path(), name).is_err());
            }
        }
        #[test]
        fn merge_preserves_other_database_keys_and_image_material() {
            let mut store = wx_keychain::KeyStore::default();
            store.set(
                "synthetic-account",
                &hex::encode([1u8; 32]),
                "test",
                None,
                None,
            );
            store.set_image_key("synthetic-account", &hex::encode([2u8; 16]));
            store.set_enc_keys(
                "synthetic-account",
                &[wx_decrypt::EncKeyPair {
                    key: [3; 32],
                    salt: [4; 16],
                }],
                "test",
                None,
                None,
            );
            let imported = [wx_decrypt::EncKeyPair {
                key: [7; 32],
                salt: [9; 16],
            }];
            let merged = merge_pairs(store.get("synthetic-account"), &imported).unwrap();
            assert_eq!(merged.len(), 2);
            store.set_enc_keys(
                "synthetic-account",
                &merged,
                "validated-capture",
                None,
                None,
            );
            let entry = store.get("synthetic-account").unwrap();
            assert_eq!(entry.data_key, hex::encode([1u8; 32]));
            assert_eq!(entry.image_aes_key, Some(hex::encode([2u8; 16])));
            assert_eq!(entry.enc_keys.len(), 2);
        }
    }
}

#[cfg(target_os = "macos")]
pub fn cmd_key_import_capture(
    capture_file: &std::path::Path,
    db_root: &std::path::Path,
) -> Result<(), Box<dyn std::error::Error>> {
    capture_import::run(capture_file, db_root)
}

#[cfg(target_os = "macos")]
pub fn cmd_key_scan_trial(
    pid: u32,
    trial_app: &std::path::Path,
) -> Result<(), Box<dyn std::error::Error>> {
    let version = trial_guard::validate(pid, trial_app)?;
    let accounts =
        wx_keychain::find_account_dirs().map_err(|_| "trial_account_discovery_failed")?;
    if accounts.is_empty() {
        return Err("trial_no_accounts".into());
    }
    let results = wx_keychain::capture_key_mach(pid, &accounts, &wx_decrypt::MACOS_4_1_7_31)
        .map_err(|error| match error {
            wx_keychain::KeychainError::TaskForPidFailed { kr, .. } => {
                format!("trial_attach_denied_kern_return_{kr}")
            }
            wx_keychain::KeychainError::NoKeysFound => "trial_no_validated_keys_found".to_owned(),
            _ => "trial_capture_failed".to_owned(),
        })?;
    // Metadata is revalidated before persisting; a replaced or stopped process fails closed.
    trial_guard::validate(pid, trial_app)?;
    let mut store = wx_keychain::KeyStore::load_default().map_err(|_| "trial_store_load_failed")?;
    let mut pairs_count = 0;
    for result in &results {
        let wx_decrypt::KeyMaterial::EncKeys(pairs) = &result.key_material else {
            return Err("trial_key_material_invalid".into());
        };
        pairs_count += pairs.len();
        store.set_enc_keys(
            &result.matched_account.account_id,
            pairs,
            &version,
            None,
            Some(result.matched_account.base_wxid.clone()),
        );
    }
    trial_guard::save_private(&store)?;
    println!(
        "{}",
        serde_json::json!({"validatedPairs":pairs_count,"matchedAccounts":results.len(),"savedPrivately":true})
    );
    Ok(())
}

use crate::util::{lookup_or_resolve_nickname, parse_hex_key_32};

pub async fn cmd_key_extract(timeout_secs: u64) -> Result<(), Box<dyn std::error::Error>> {
    eprintln!("Running pre-flight checks...");
    wx_keychain::preflight_checks()?;
    eprintln!("  All checks passed.");

    let version = wx_keychain::ensure_supported_wechat_version()?;
    eprintln!("  WeChat version: {version}");

    let accounts = wx_keychain::find_account_dirs()?;
    if accounts.is_empty() {
        return Err("no WeChat account directories found".into());
    }

    let mut store = wx_keychain::KeyStore::load_default()?;
    let mut store_dirty = false;

    eprintln!("Detected accounts:");
    for a in &accounts {
        let nick = lookup_or_resolve_nickname(&mut store, a);
        if nick.is_some() {
            store_dirty = true;
        }
        eprintln!(
            "  {} ({})",
            a.account_id,
            nick.unwrap_or_else(|| "昵称未知".to_string())
        );
    }
    if store_dirty {
        store.save_default()?;
    }

    let result = wx_keychain::capture_key(&accounts, Duration::from_secs(timeout_secs)).await?;

    let matched = &result.matched_account;
    let hex_key = hex::encode(result.raw_key);
    eprintln!("Key captured after {} PBKDF2 calls.", result.call_count);
    eprintln!("Matched account: {}", matched.account_id);
    println!("{hex_key}");

    let nickname = wx_keychain::resolve_nickname(
        &matched.data_dir,
        &wx_decrypt::KeyMaterial::RawKey(result.raw_key),
        &matched.base_wxid,
    )
    .unwrap_or_else(|e| {
        eprintln!("  Warning: nickname resolution failed: {e}");
        None
    });

    if let Some(ref n) = nickname {
        eprintln!("Account nickname: {n}");
    }

    store.set(
        &matched.account_id,
        &hex_key,
        &version,
        nickname,
        Some(matched.base_wxid.clone()),
    );
    store.save_default()?;
    eprintln!("Key saved to {:?}", wx_keychain::KeyStore::default_path()?);

    Ok(())
}

#[cfg(target_os = "macos")]
pub fn cmd_key_scan() -> Result<(), Box<dyn std::error::Error>> {
    // SIP check — task_for_pid fails with kern_return=5 when SIP is enabled,
    // even as root. This is a hard requirement (tested 2026-03-08).
    let sip = wx_keychain::check_sip();
    if !sip.passed {
        return Err(format!(
            "{} — original-process scanning is unavailable. Import a locally validated capture with key import-capture; no system protection was changed",
            sip.detail
        )
        .into());
    }

    // Find WeChat process (PID + version only).
    let (pid, version) = wx_keychain::find_wechat_pid()?;
    eprintln!("Found WeChat PID {} (v{})", pid, version);

    // Load account directories.
    let accounts = wx_keychain::find_account_dirs()?;
    if accounts.is_empty() {
        return Err("no WeChat account directories found".into());
    }
    eprintln!(
        "Found {} account director{}",
        accounts.len(),
        if accounts.len() == 1 { "y" } else { "ies" }
    );

    // Scan process memory.
    eprintln!("Scanning WeChat process memory...");
    let results = wx_keychain::capture_key_mach(pid, &accounts, &wx_decrypt::MACOS_4_1_7_31)?;

    // Count total pairs across all results
    let total_pairs: usize = results
        .iter()
        .map(|r| match &r.key_material {
            wx_decrypt::KeyMaterial::EncKeys(pairs) => pairs.len(),
            _ => unreachable!("capture_key_mach always returns EncKeys"),
        })
        .sum();
    eprintln!(
        "Found {} valid key{} for {} account{}",
        total_pairs,
        if total_pairs == 1 { "" } else { "s" },
        results.len(),
        if results.len() == 1 { "" } else { "s" },
    );

    // Store results.
    let mut store = wx_keychain::KeyStore::load_default()?;
    for r in &results {
        let matched = &r.matched_account;

        let nickname =
            wx_keychain::resolve_nickname(&matched.data_dir, &r.key_material, &matched.base_wxid)
                .unwrap_or_else(|e| {
                    eprintln!(
                        "  Warning: nickname resolution failed for {}: {e}",
                        matched.account_id
                    );
                    None
                });

        let pairs = match &r.key_material {
            wx_decrypt::KeyMaterial::EncKeys(pairs) => pairs,
            _ => unreachable!("capture_key_mach always returns EncKeys"),
        };

        store.set_enc_keys(
            &matched.account_id,
            pairs,
            &version,
            nickname.clone(),
            Some(matched.base_wxid.clone()),
        );

        let display = nickname
            .as_ref()
            .map(|n| format!("{} ({})", matched.account_id, n))
            .unwrap_or_else(|| matched.account_id.clone());
        eprintln!(
            "  {} — {} enc_key{} stored",
            display,
            pairs.len(),
            if pairs.len() == 1 { "" } else { "s" }
        );
        for pair in pairs {
            println!(
                "{}\t{}\t{}",
                matched.account_id,
                hex::encode(pair.key),
                hex::encode(pair.salt)
            );
        }
    }

    store.save_default()?;
    eprintln!("Keys saved to {:?}", wx_keychain::KeyStore::default_path()?);

    Ok(())
}

#[cfg(not(target_os = "macos"))]
pub fn cmd_key_scan() -> Result<(), Box<dyn std::error::Error>> {
    Err("key scan is only supported on macOS".into())
}

pub fn cmd_key_list() -> Result<(), Box<dyn std::error::Error>> {
    let mut store = wx_keychain::KeyStore::load_default()?;
    if store.accounts.is_empty() {
        eprintln!("No keys stored.");
        return Ok(());
    }

    let accounts = wx_keychain::find_account_dirs().unwrap_or_default();
    let mut store_dirty = false;
    for account in &accounts {
        if lookup_or_resolve_nickname(&mut store, account).is_some() {
            store_dirty = true;
        }
    }
    if store_dirty {
        store.save_default()?;
    }

    let mut ids: Vec<_> = store.accounts.keys().cloned().collect();
    ids.sort();
    for id in ids {
        let key = store
            .get(&id)
            .ok_or_else(|| format!("missing key entry for account {id}"))?;

        let raw_status = if key.data_key.is_empty() { "no" } else { "yes" };
        let enc_count = key.enc_keys.len();
        let has_legacy_enc = key.enc_key.as_ref().is_some_and(|k| !k.is_empty());
        let enc_status = if enc_count > 0 {
            format!("{enc_count}")
        } else if has_legacy_enc {
            "1".to_string()
        } else {
            "no".to_string()
        };
        let img_status = if key.image_aes_key.is_some() {
            "yes"
        } else {
            "no"
        };

        let display_key = if !key.data_key.is_empty() {
            key.data_key.clone()
        } else if enc_count > 0 {
            let first = &key.enc_keys[0].enc_key;
            if enc_count > 1 {
                format!("{} (+{} more)", first, enc_count - 1)
            } else {
                first.clone()
            }
        } else if let Some(ref ek) = key.enc_key {
            ek.clone()
        } else {
            "(no key)".to_string()
        };

        println!(
            "{}  {}  (v{}, {}, raw={} enc={} img={})",
            key.display_name(),
            display_key,
            key.wechat_version,
            key.extracted_at.format("%Y-%m-%d %H:%M:%S UTC"),
            raw_status,
            enc_status,
            img_status,
        );
    }
    Ok(())
}

pub fn cmd_key_set(account: &str, hex_key: &str) -> Result<(), Box<dyn std::error::Error>> {
    parse_hex_key_32(hex_key, "manual key")?;

    let mut store = wx_keychain::KeyStore::load_default()?;
    store.set(account, hex_key, "manual", None, None);
    store.save_default()?;
    eprintln!("Key saved for {account}.");
    Ok(())
}

pub fn cmd_key_set_image(account: &str, image_key: &str) -> Result<(), Box<dyn std::error::Error>> {
    let key_hex = if image_key.len() == 32 && image_key.chars().all(|c| c.is_ascii_hexdigit()) {
        image_key.to_string()
    } else if image_key.len() == 16 && image_key.is_ascii() {
        hex::encode(image_key.as_bytes())
    } else {
        return Err(format!(
            "image key must be 16-byte ASCII string or 32-char hex, got {} chars",
            image_key.len()
        )
        .into());
    };

    let mut store = wx_keychain::KeyStore::load_default()?;
    store.set_image_key(account, &key_hex);
    store.save_default()?;
    eprintln!("Image AES key saved for {account}.");
    Ok(())
}
