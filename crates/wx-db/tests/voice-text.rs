use wx_db::{decode_message_for_test, MessageContent};

#[test]
fn compressed_group_voice_keeps_native_text_without_credentials() {
    let xml = r#"wxid_fixture:
<msg><voicemsg aeskey="PRIVATE" voiceurl="PRIVATE"/><voicetrans transtext="明天三点开会 &amp; 确认"/></msg>"#;
    let compressed = zstd::encode_all(xml.as_bytes(), 1).unwrap();
    let message = decode_message_for_test(
        1000,
        12345,
        34,
        "fallback",
        "fixture@chatroom",
        1,
        &compressed,
        None,
        0,
        Some(4),
        None,
        true,
    )
    .unwrap();
    assert_eq!(message.sender, "wxid_fixture");
    let value = serde_json::to_string(&message.content).unwrap();
    assert!(value.contains("明天三点开会 & 确认"));
    assert!(!value.contains("PRIVATE"));
    assert!(matches!(
        message.content,
        MessageContent::VoiceTranscript { .. }
    ));
}

#[test]
fn voice_without_native_text_keeps_existing_serialization() {
    let message = decode_message_for_test(
        1000,
        12345,
        34,
        "fixture",
        "fixture",
        1,
        b"<msg><voicemsg aeskey=\"PRIVATE\"/></msg>",
        None,
        0,
        None,
        None,
        false,
    )
    .unwrap();
    assert_eq!(
        serde_json::to_string(&message.content).unwrap(),
        "\"Voice\""
    );
}
