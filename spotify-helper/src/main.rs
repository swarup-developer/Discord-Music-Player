//! Spotify player for the Discord music bot.
//!
//!   dcbot-spotify pair  <dir>   pair-code login; the login is stored in <dir>
//!   dcbot-spotify serve <dir>   play with the stored login
//!
//! serve: JSON commands on stdin, one per line; raw PCM (s16le, 44100 Hz,
//! stereo) on stdout in real time (silence while nothing plays); JSON events
//! on stderr, one per line (other stderr lines are logs).
//!
//! Built on librespot. Commands:
//!   {"cmd":"load","uri":"spotify:track:..","pos":0,"play":true}
//!   {"cmd":"play"} {"cmd":"pause"} {"cmd":"stop"} {"cmd":"seek","pos":ms}
//!   {"cmd":"preload","uri":".."}
//!   {"cmd":"search","id":1,"q":"text","limit":5}
//!   {"cmd":"resolve","id":2,"q":"spotify link, URI or text"}

use std::collections::VecDeque;
use std::io::Write;
use std::path::{Path, PathBuf};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::Arc;
use std::time::{Duration, Instant};

use crossbeam_channel::{bounded, Receiver, Sender};
use librespot_core::authentication::Credentials;
use librespot_core::cache::Cache;
use librespot_core::config::SessionConfig;
use librespot_core::session::Session;
use librespot_core::spotify_uri::SpotifyUri;
use librespot_metadata::{Album, Metadata, Playlist, Track};
use librespot_playback::audio_backend::{Sink, SinkError, SinkResult};
use librespot_playback::config::{Bitrate, PlayerConfig};
use librespot_playback::convert::Converter;
use librespot_playback::decoder::AudioPacket;
use librespot_playback::mixer::NoOpVolume;
use librespot_playback::player::{Player, PlayerEvent};
use serde_json::{json, Value};
use tokio::io::{AsyncBufReadExt, BufReader};

const CLIENT_ID: &str = "65b708073fc0480ea92a077233ca87bd";
const SCOPES: &[&str] = &[
    "streaming",
    "user-read-playback-state",
    "user-modify-playback-state",
    "user-read-currently-playing",
];
const RATE: usize = 44_100;
const FRAME_SAMPLES: usize = RATE / 50 * 2; // 20 ms, stereo

fn emit(v: Value) {
    let mut err = std::io::stderr().lock();
    let _ = writeln!(err, "{v}");
}

fn cache_for(dir: &Path) -> Cache {
    let creds = dir.join("auth");
    let audio = dir.join("audio");
    Cache::new(Some(creds), Some(dir.to_path_buf()), Some(audio), Some(200 * 1024 * 1024))
        .expect("cache dir")
}

fn account(session: &Session) -> Value {
    json!({
        "user": session.username(),
        "product": session.get_user_attribute("type").unwrap_or_default(),
    })
}

// ------------------------------------------------------------------ pair --

async fn pair(dir: PathBuf) -> i32 {
    let client = match librespot_oauth::DeviceAuthClientBuilder::new(CLIENT_ID, SCOPES.to_vec()).build() {
        Ok(c) => c,
        Err(e) => {
            emit(json!({"ev":"error","message":format!("pair setup: {e}")}));
            return 1;
        }
    };
    let auth = match client.request_device_code_async().await {
        Ok(a) => a,
        Err(e) => {
            emit(json!({"ev":"error","message":format!("Spotify gave no pairing code: {e}")}));
            return 1;
        }
    };
    emit(json!({"ev":"code","url":auth.url(),"code":auth.user_code(),"page":auth.verification_uri()}));
    let token = match client.poll_for_token_async(&auth).await {
        Ok(t) => t,
        Err(e) => {
            emit(json!({"ev":"error","message":format!("pairing not confirmed: {e}")}));
            return 1;
        }
    };
    let session = Session::new(SessionConfig::default(), Some(cache_for(&dir)));
    if let Err(e) = session.connect(Credentials::with_access_token(&token.access_token), true).await {
        emit(json!({"ev":"error","message":format!("Spotify sign-in failed: {e}")}));
        return 1;
    }
    let mut ok = account(&session);
    ok["ev"] = json!("paired");
    emit(ok);
    0
}

// ------------------------------------------------------------------ sink --

struct PipeSink {
    tx: Sender<Vec<i16>>,
}

impl Sink for PipeSink {
    fn write(&mut self, packet: AudioPacket, converter: &mut Converter) -> SinkResult<()> {
        if let AudioPacket::Samples(samples) = packet {
            self.tx
                .send(converter.f64_to_s16(&samples))
                .map_err(|e| SinkError::OnWrite(e.to_string()))?;
        }
        Ok(())
    }
}

/// Writes PCM to stdout at real-time pace, silence when nothing is queued,
/// so Discord gets a steady stream. A bump of `flush` drops what is buffered
/// (after load, seek and stop).
fn writer(rx: Receiver<Vec<i16>>, flush: Arc<AtomicU64>) {
    let mut out = std::io::stdout().lock();
    let mut buf: VecDeque<i16> = VecDeque::with_capacity(RATE * 2);
    let mut seen = flush.load(Ordering::Relaxed);
    let mut frame = vec![0i16; FRAME_SAMPLES];
    let mut bytes = vec![0u8; FRAME_SAMPLES * 2];
    let tick = Duration::from_millis(20);
    let mut next = Instant::now();
    loop {
        let now_flush = flush.load(Ordering::Relaxed);
        if now_flush != seen {
            seen = now_flush;
            buf.clear();
            while rx.try_recv().is_ok() {}
        }
        while buf.len() < FRAME_SAMPLES {
            match rx.try_recv() {
                Ok(chunk) => buf.extend(chunk),
                Err(_) => break,
            }
        }
        for s in frame.iter_mut() {
            *s = buf.pop_front().unwrap_or(0);
        }
        for (i, s) in frame.iter().enumerate() {
            let b = s.to_le_bytes();
            bytes[i * 2] = b[0];
            bytes[i * 2 + 1] = b[1];
        }
        if out.write_all(&bytes).is_err() {
            std::process::exit(0); // the bot closed the stream
        }
        let _ = out.flush();
        next += tick;
        let now = Instant::now();
        if next > now {
            std::thread::sleep(next - now);
        } else if now - next > Duration::from_millis(200) {
            next = now; // fell behind (machine busy): don't burst to catch up
        }
    }
}

// -------------------------------------------------------------- metadata --

fn image_url(images: &librespot_metadata::image::Images) -> Option<String> {
    images
        .0
        .iter()
        .max_by_key(|i| i.width)
        .map(|i| format!("https://i.scdn.co/image/{}", i.id.to_base16()))
}

fn track_json(t: &Track) -> Value {
    json!({
        "uri": t.id.to_uri(),
        "name": t.name,
        "artists": t.artists.0.iter().map(|a| a.name.clone()).collect::<Vec<_>>(),
        "album": t.album.name,
        "duration_ms": t.duration,
        "image": image_url(&t.album.covers),
    })
}

async fn tracks_meta(session: &Session, uris: &[SpotifyUri]) -> Vec<Value> {
    let futs = uris.iter().cloned().map(|u| {
        let session = session.clone();
        async move { Track::get(&session, &u).await }
    });
    futures_join(futs).await.into_iter().filter_map(|r| r.ok()).map(|t| track_json(&t)).collect()
}

async fn futures_join<F, T>(futs: impl Iterator<Item = F>) -> Vec<T>
where
    F: std::future::Future<Output = T> + Send + 'static,
    T: Send + 'static,
{
    let handles: Vec<_> = futs.map(tokio::spawn).collect();
    let mut out = Vec::with_capacity(handles.len());
    for h in handles {
        if let Ok(v) = h.await {
            out.push(v);
        }
    }
    out
}

fn search_uri(q: &str) -> String {
    use percent_encoding::{utf8_percent_encode, AsciiSet, NON_ALPHANUMERIC};
    const SET: &AsciiSet = &NON_ALPHANUMERIC.remove(b'-').remove(b'_').remove(b'.').remove(b'~');
    let words: Vec<String> = q.split_whitespace().map(|w| utf8_percent_encode(w, SET).to_string()).collect();
    format!("spotify:search:{}", words.join("+"))
}

async fn search(session: &Session, q: &str, limit: usize) -> Result<Vec<Value>, String> {
    let ctx = session.spclient().get_context(&search_uri(q)).await.map_err(|e| e.to_string())?;
    let uris: Vec<SpotifyUri> = ctx
        .pages
        .iter()
        .flat_map(|p| p.tracks.iter())
        .filter_map(|t| SpotifyUri::from_uri(t.uri.as_deref()?).ok())
        .filter(|u| matches!(u, SpotifyUri::Track { .. }))
        .take(limit + 3)
        .collect();
    let mut out = tracks_meta(session, &uris).await;
    out.truncate(limit);
    Ok(out)
}

/// "https://open.spotify.com/intl-xx/track/ID?si=.." or "spotify:track:ID".
fn parse_ref(q: &str) -> Option<String> {
    let q = q.trim();
    if q.starts_with("spotify:") {
        return Some(q.to_string());
    }
    let rest = q.split("open.spotify.com/").nth(1)?;
    let mut parts = rest.split(['/', '?', '#']).filter(|p| !p.is_empty() && !p.starts_with("intl-"));
    let kind = parts.next()?;
    let id = parts.next()?;
    Some(format!("spotify:{kind}:{id}"))
}

async fn resolve(session: &Session, q: &str) -> Result<Value, String> {
    let Some(uri_s) = parse_ref(q) else {
        let tracks = search(session, q, 1).await?;
        return Ok(json!({"kind":"search","title":q,"tracks":tracks,"rest":[]}));
    };
    let uri = SpotifyUri::from_uri(&uri_s).map_err(|e| e.to_string())?;
    match uri {
        SpotifyUri::Track { .. } => {
            let t = Track::get(session, &uri).await.map_err(|e| e.to_string())?;
            Ok(json!({"kind":"track","title":t.name,"tracks":[track_json(&t)],"rest":[]}))
        }
        SpotifyUri::Album { .. } => {
            let a = Album::get(session, &uri).await.map_err(|e| e.to_string())?;
            let uris: Vec<SpotifyUri> = a.tracks().cloned().collect();
            let first = tracks_meta(session, &uris[..uris.len().min(25)]).await;
            let rest: Vec<String> = uris.iter().skip(25).map(|u| u.to_uri()).collect();
            Ok(json!({"kind":"album","title":a.name,"image":image_url(&a.covers),"tracks":first,"rest":rest}))
        }
        SpotifyUri::Playlist { .. } => {
            let p = Playlist::get(session, &uri).await.map_err(|e| e.to_string())?;
            let uris: Vec<SpotifyUri> = p
                .tracks()
                .filter(|u| matches!(u, SpotifyUri::Track { .. }))
                .take(500)
                .cloned()
                .collect();
            let first = tracks_meta(session, &uris[..uris.len().min(25)]).await;
            let rest: Vec<String> = uris.iter().skip(25).map(|u| u.to_uri()).collect();
            Ok(json!({"kind":"playlist","title":p.name(),"tracks":first,"rest":rest}))
        }
        _ => Err("only tracks, albums and playlists can be played".into()),
    }
}

// ----------------------------------------------------------------- serve --

async fn serve(dir: PathBuf) -> i32 {
    let cache = cache_for(&dir);
    let Some(creds) = cache.credentials() else {
        emit(json!({"ev":"error","message":"not logged in"}));
        return 2;
    };
    let session = Session::new(SessionConfig::default(), Some(cache));
    if let Err(e) = session.connect(creds, true).await {
        emit(json!({"ev":"error","message":format!("Spotify sign-in failed: {e}")}));
        return 3;
    }

    let (tx, rx) = bounded::<Vec<i16>>(64);
    let flush = Arc::new(AtomicU64::new(0));
    {
        let flush = flush.clone();
        std::thread::spawn(move || writer(rx, flush));
    }
    let config = PlayerConfig { bitrate: Bitrate::Bitrate320, gapless: true, ..Default::default() };
    let player = Player::new(config, session.clone(), Box::new(NoOpVolume), move || {
        Box::new(PipeSink { tx }) as Box<dyn Sink>
    });
    let mut events = player.get_player_event_channel();
    tokio::spawn(async move {
        while let Some(ev) = events.recv().await {
            let u = |t: &SpotifyUri| t.to_uri();
            let v = match ev {
                PlayerEvent::Playing { track_id, position_ms, .. } => json!({"ev":"playing","uri":u(&track_id),"pos":position_ms}),
                PlayerEvent::Paused { track_id, position_ms, .. } => json!({"ev":"paused","uri":u(&track_id),"pos":position_ms}),
                PlayerEvent::PositionChanged { track_id, position_ms, .. }
                | PlayerEvent::Seeked { track_id, position_ms, .. }
                | PlayerEvent::PositionCorrection { track_id, position_ms, .. } => json!({"ev":"pos","uri":u(&track_id),"pos":position_ms}),
                PlayerEvent::Loading { track_id, .. } => json!({"ev":"loading","uri":u(&track_id)}),
                PlayerEvent::EndOfTrack { track_id, .. } => json!({"ev":"end","uri":u(&track_id)}),
                PlayerEvent::Unavailable { track_id, .. } => json!({"ev":"unavailable","uri":u(&track_id)}),
                PlayerEvent::Stopped { track_id, .. } => json!({"ev":"stopped","uri":u(&track_id)}),
                PlayerEvent::TimeToPreloadNextTrack { track_id, .. } => json!({"ev":"preload_next","uri":u(&track_id)}),
                PlayerEvent::SessionDisconnected { .. } => json!({"ev":"disconnected"}),
                _ => continue,
            };
            emit(v);
        }
    });

    let mut ready = account(&session);
    ready["ev"] = json!("ready");
    emit(ready);

    let mut lines = BufReader::new(tokio::io::stdin()).lines();
    while let Ok(Some(line)) = lines.next_line().await {
        let Ok(cmd) = serde_json::from_str::<Value>(&line) else { continue };
        let id = cmd.get("id").cloned().unwrap_or(Value::Null);
        let s = |k: &str| cmd.get(k).and_then(|v| v.as_str()).unwrap_or("").to_string();
        let n = |k: &str| cmd.get(k).and_then(|v| v.as_u64()).unwrap_or(0);
        match cmd.get("cmd").and_then(|v| v.as_str()).unwrap_or("") {
            "load" => match SpotifyUri::from_uri(&s("uri")) {
                Ok(uri) => {
                    flush.fetch_add(1, Ordering::Relaxed);
                    let play = cmd.get("play").and_then(|v| v.as_bool()).unwrap_or(true);
                    player.load(uri, play, n("pos") as u32);
                }
                Err(e) => emit(json!({"ev":"error","id":id,"message":e.to_string()})),
            },
            "preload" => {
                if let Ok(uri) = SpotifyUri::from_uri(&s("uri")) {
                    player.preload(uri);
                }
            }
            "play" => player.play(),
            "pause" => player.pause(),
            "stop" => {
                player.stop();
                flush.fetch_add(1, Ordering::Relaxed);
            }
            "seek" => {
                flush.fetch_add(1, Ordering::Relaxed);
                player.seek(n("pos") as u32);
            }
            "search" | "resolve" => {
                let session = session.clone();
                let q = s("q");
                let kind = s("cmd");
                let limit = n("limit").clamp(1, 20) as usize;
                tokio::spawn(async move {
                    let r = if kind == "search" {
                        search(&session, &q, limit).await.map(|t| json!({"tracks": t}))
                    } else {
                        resolve(&session, &q).await
                    };
                    match r {
                        Ok(mut v) => {
                            v["ev"] = json!("result");
                            v["id"] = id;
                            emit(v);
                        }
                        Err(e) => emit(json!({"ev":"result","id":id,"error":e})),
                    }
                });
            }
            "meta" => {
                let session = session.clone();
                let uris: Vec<SpotifyUri> = cmd
                    .get("uris")
                    .and_then(|v| v.as_array())
                    .map(|a| a.iter().filter_map(|u| SpotifyUri::from_uri(u.as_str()?).ok()).collect())
                    .unwrap_or_default();
                tokio::spawn(async move {
                    let t = tracks_meta(&session, &uris).await;
                    emit(json!({"ev":"result","id":id,"tracks":t}));
                });
            }
            "quit" => break,
            _ => {}
        }
    }
    0
}

#[tokio::main]
async fn main() {
    env_logger::Builder::from_env(env_logger::Env::default().default_filter_or("warn")).init();
    let args: Vec<String> = std::env::args().collect();
    let (Some(mode), Some(dir)) = (args.get(1), args.get(2)) else {
        eprintln!("usage: dcbot-spotify pair|serve <dir>");
        std::process::exit(64);
    };
    let dir = PathBuf::from(dir);
    let _ = std::fs::create_dir_all(&dir);
    let code = match mode.as_str() {
        "pair" => pair(dir).await,
        "serve" => serve(dir).await,
        _ => 64,
    };
    std::process::exit(code);
}
