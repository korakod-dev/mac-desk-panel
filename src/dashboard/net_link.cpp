#include "net_link.h"

#include <HTTPClient.h>
#include <WiFi.h>
#include <WiFiClientSecure.h>
#include <sys/time.h>

#include "secrets.h"

// --- wire format -------------------------------------------------------------
//
//   device -> host    \n@REQ <id> <verb> [arg]\n
//   host   -> device   @RES <id> <status> <len>\n<len bytes>
//
// Length-framed rather than newline-terminated, so a JSON body carrying
// newlines needs no escaping and an error message can ride in the same envelope
// as a body would. The leading newline on a request guarantees the header
// starts a line even if a log line was left unterminated.
//
// Verbs: PING (liveness), TIME (epoch seconds), GET <url> (response body).

namespace {

const uint32_t PING_EVERY    = 4000;   // how often to re-assert the bridge
const uint32_t PING_TTL      = 12000;  // silence for this long means it is gone
const uint32_t GET_TIMEOUT   = 8000;   // the host still has to do TLS + fetch
const uint32_t TIME_TIMEOUT  = 1000;
// Bounded by the CDC receive queue set up in setup(): a reply larger than the
// queue can outrun the reader and lose its tail, so the bridge is told to
// refuse those outright rather than deliver half of one.
const int      MAX_BODY      = 4096;
// How long a half-read frame is given before it is abandoned. A body follows
// its header immediately and arrives in one burst, so a gap this size does not
// mean a slow host — it means the rest is not coming: the Mac suspended the bus
// mid-reply, or the bridge lost the port between the header and the body.
//
// Without it that frame is permanent. rxNeed never reaches zero, every reply
// after it is eaten as filler until the count runs out, the panel drops to
// OFFLINE with the cable still plugged in, and the only way back is a reset by
// hand. This is the whole of the difference between one dropped reply and a
// panel that has to be unplugged.
const uint32_t RX_STALL      = 2000;

// Must match TOKEN_HEADER in tools/mac_stats_server.py and usage_server.py.
const char    *TOKEN_HEADER  = "X-Panel-Token";

// Skip is Body without a length to trust: a header that did not parse, or one
// claiming more than the bridge is allowed to send. The bytes behind it still
// have to go somewhere, and the one place they must not go is the command queue
// — 'S' there dumps the framebuffer and 'P' starts the timer.
enum class Rx : uint8_t { Idle, Header, Body, Skip };

Rx       rxState = Rx::Idle;
String   rxHeader;
String  *rxSink   = nullptr;  // body destination for the request being awaited
bool     rxKeep   = false;    // false while swallowing a body nobody asked for
int      rxNeed   = 0;
int      rxStatus = 0;
uint16_t rxId     = 0;        // id `rxSink` is waiting for
bool     rxDone   = false;
uint32_t rxSince  = 0;        // when the frame being read was started

uint16_t nextId = 1;
uint16_t pingId = 0;

net::IdleHook idleHook = nullptr;
bool          inRequest = false;   // guards the single reader against nesting

uint32_t lastPing = 0, lastUsbOk = 0;
bool     usbUp = false;
// Whether there has ever been a bridge on this boot. What separates "the link
// died" from "there was never a link", which look identical from the silence
// alone and want opposite things done about them.
bool     everUp = false;

// Room for a few keystrokes; the UI only ever reads single-byte commands.
uint8_t cmdBuf[16];
uint8_t cmdHead = 0, cmdTail = 0;

void pushCommand(uint8_t c) {
  uint8_t next = (uint8_t)((cmdHead + 1) % sizeof(cmdBuf));
  if (next == cmdTail) return;  // full — drop, the UI is not keeping up
  cmdBuf[cmdHead] = c;
  cmdHead = next;
}

uint16_t claimId() {
  uint16_t id = nextId++;
  if (nextId == 0) nextId = 1;
  return id;
}

// A complete "@RES ..." header line is in rxHeader. Decide whether the body
// that follows is wanted, and switch to reading it either way — the bytes have
// to be consumed regardless, or they would be mistaken for console input.
void beginBody() {
  rxState = Rx::Idle;
  rxKeep  = false;
  rxNeed  = 0;

  unsigned id = 0;
  int status = 0, len = 0;
  // Both of these mean the header is not one the bridge wrote, so the length in
  // it cannot be used to find the end of the body either. Skip discards what
  // follows until the wire goes quiet, which is the closest thing to a frame
  // boundary there is when the framing itself is what went wrong.
  if (sscanf(rxHeader.c_str(), "@RES %u %d %d", &id, &status, &len) != 3 ||
      len < 0 || len > MAX_BODY) {
    rxState = Rx::Skip;
    rxSince = millis();
    return;
  }

  if (id == pingId) {
    lastUsbOk = millis();
    usbUp     = true;
    everUp    = true;
    pingId    = 0;
  } else if (rxSink && id == rxId) {
    rxKeep   = true;
    rxStatus = status;
    // The body lands in 128-byte reads, and String grows by reallocating: a
    // 4 KB reply would otherwise copy itself through some thirty allocations
    // of ascending size, which is how a panel left up for weeks arrives at a
    // heap too fragmented to hold the next one. The length is in the header,
    // so the whole body can be one allocation.
    rxSink->reserve(len);
  }

  rxNeed  = len;
  rxState = Rx::Body;
  rxSince = millis();
  if (rxNeed == 0) {
    rxState = Rx::Idle;
    if (rxKeep) rxDone = true;
  }
}

void pump() {
  // Nothing below can finish a frame whose remaining bytes are never going to
  // arrive, so the way out is here rather than inside any one of them. Idle is
  // the state it is safe to be wrong in: at worst a late tail is read as
  // console input and dropped, and the next '@' picks the framing back up.
  if (rxState != Rx::Idle && millis() - rxSince > RX_STALL) {
    const char *what = rxState == Rx::Body   ? "body"
                     : rxState == Rx::Header ? "header"
                                             : "unparsed frame";
    Serial.printf("[net] resync: %s abandoned after %lums\n", what,
                  (unsigned long)RX_STALL);
    rxState = Rx::Idle;
    rxKeep  = false;
    rxNeed  = 0;
  }

  for (;;) {
    if (rxState == Rx::Body) {
      while (rxNeed > 0) {
        uint8_t buf[128];
        int want = rxNeed < (int)sizeof(buf) ? rxNeed : (int)sizeof(buf);
        // read(), not readBytes(): HWCDC leaves readBytes to Stream, which
        // spins for its full one-second timeout waiting for a byte that a
        // truncated body is never going to send — and it spins without
        // yielding. Two pumps a pass through loop() is two seconds a pass, and
        // a panel polled at 0.5 Hz cannot debounce a button press, which is
        // what made a lost reply look like a dead panel rather than a stale
        // one. This one returns what is in the queue and nothing more.
        int n = (int)Serial.read(buf, (size_t)want);
        if (n <= 0) return;  // partial body; pick it up on the next pump
        if (rxKeep && rxSink) rxSink->concat(buf, (unsigned int)n);
        rxNeed -= n;
      }
      rxState = Rx::Idle;
      if (rxKeep) {
        rxDone = true;
        rxKeep = false;
      }
    }

    if (rxState == Rx::Skip) {
      uint8_t buf[128];
      // Everything available, thrown away. The deadline above is what ends it.
      while ((int)Serial.read(buf, sizeof(buf)) > 0) {}
      return;
    }

    if (!Serial.available()) return;
    int c = Serial.read();
    if (c < 0) return;

    if (rxState == Rx::Header) {
      if (c == '\n') {
        beginBody();
      } else if (rxHeader.length() < 64) {
        rxHeader += (char)c;
      } else {
        rxState = Rx::Idle;  // runaway line — resynchronise
      }
      continue;
    }

    // Idle: '@' opens a response header, everything else is console input.
    if (c == '@') {
      rxHeader = "@";
      rxState  = Rx::Header;
      rxSince  = millis();
    } else if (c != '\r' && c != '\n') {
      pushCommand((uint8_t)c);
    }
  }
}

// Sends a request and blocks until the matching response lands. `out` is
// cleared first, so a timeout leaves it empty rather than half-filled.
bool request(const char *verb, const String &arg, uint32_t timeout, String &out,
             int &status) {
  // There is one reader and one sink, so a request started from inside the idle
  // hook of another would answer the wrong caller. The hook is documented not
  // to fetch; this makes the mistake fail cleanly instead of subtly.
  if (inRequest) return false;
  inRequest = true;

  pump();  // drain anything left over before claiming the reader

  out      = "";
  rxId     = claimId();
  rxSink   = &out;
  rxDone   = false;
  rxStatus = 0;

  if (arg.length()) {
    Serial.printf("\n@REQ %u %s %s\n", rxId, verb, arg.c_str());
  } else {
    Serial.printf("\n@REQ %u %s\n", rxId, verb);
  }
  Serial.flush();

  uint32_t start = millis();
  while (!rxDone && millis() - start < timeout) {
    pump();
    if (rxDone) break;
    // The panel is otherwise dead for the whole of this wait. The hook redraws
    // it and watches the buttons; pump() above keeps the reply draining, and
    // the CDC queue was sized in setup() to hold a whole body, so a redraw
    // landing mid-response costs latency rather than bytes.
    if (idleHook) idleHook();
    delay(2);
  }

  rxSink = nullptr;
  status = rxStatus;
  inRequest = false;

  if (!rxDone) {
    usbUp = false;
    return false;
  }
  lastUsbOk = millis();
  usbUp     = true;
  everUp    = true;
  return true;
}

// Liveness is asserted without blocking: the ping goes out and the answer is
// picked up by whichever pump() call happens to see it. A blocking probe would
// stall the clock for its whole timeout every time no bridge is running.
void sendPing() {
  pingId = claimId();
  Serial.printf("\n@REQ %u PING\n", pingId);
}

bool wifiGet(const String &url, String &out, String &err, uint32_t timeout,
             const char *token) {
  bool secure = url.startsWith("https:");

  WiFiClientSecure tls;
  WiFiClient plain;
  if (secure) {
    tls.setInsecure();  // public endpoints only; no cert pinning to maintain
    tls.setTimeout(timeout / 1000);
  }

  HTTPClient http;
  bool begun = secure ? http.begin(tls, url) : http.begin(plain, url);
  if (!begun) {
    err = "WiFi: connect failed";
    return false;
  }
  http.setTimeout(timeout);
  // A host that is not there has to cost what a slow one costs. Left unset, the
  // connect falls back to WiFiClient's own default rather than the budget the
  // caller asked for, so the one request nothing here counted on is the one to
  // an address that stopped answering — which is the ordinary state of the
  // Mac's LAN address, since that machine moves between networks.
  http.setConnectTimeout((int32_t)timeout);
  if (token && *token) http.addHeader(TOKEN_HEADER, token);

  // The USB path feeds the watchdog and redraws the panel from inside its own
  // wait. This one cannot: HTTPClient offers nothing to hook, and the call
  // below owns the CPU until it answers or gives up. So the hook is called on
  // the way in and on the way out instead.
  //
  // That is not the clock ticking through the wait — it still stops for the
  // length of the request — but it is what keeps a pass carrying several of
  // them from adding up to a watchdog reset, because each one starts the timer
  // afresh. loop() chains up to five, and clears three of them together every
  // time a link comes up. See the watchdog note in main.cpp.
  if (idleHook) idleHook();

  int status = http.GET();
  if (status != HTTP_CODE_OK) {
    err = "WiFi: HTTP " + String(status);
    http.end();
    return false;
  }

  // getString(), not getStream() — Open-Meteo answers with Transfer-Encoding:
  // chunked, and only getString() strips the chunk framing. Feeding the raw
  // stream to ArduinoJson makes it choke on the leading chunk-size line.
  if (idleHook) idleHook();

  out = http.getString();
  http.end();

  if (idleHook) idleHook();
  return true;
}

}  // namespace

namespace net {

void begin() {
  if (strcmp(WIFI_SSID, "YOUR_WIFI_NAME") == 0) {
    Serial.println("[net] secrets.h still has WiFi placeholders - USB only");
    return;
  }
  Serial.printf("[net] wifi: connecting to %s\n", WIFI_SSID);
  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);
}

void loop() {
  pump();

  if (usbUp && millis() - lastUsbOk > PING_TTL) {
    usbUp = false;
    Serial.println("[net] usb bridge stopped answering");
  }

  // Sent without asking HWCDC whether anyone is listening. It drops the write
  // when the host is absent, which is all the check was ever buying — and the
  // check itself can latch: `connected` is a guess made from SOF interrupts and
  // an IN_EMPTY that is only re-armed once, so a suspend and resume can leave it
  // false with the cable in and the bridge running. The host speaks only when
  // spoken to, so a panel that stops asking is a panel that never hears anything
  // again. Better to spend a few bytes on a cable with nothing at the end of it.
  if (millis() - lastPing >= PING_EVERY) {
    lastPing = millis();
    sendPing();
  }
}

Via via() {
  if (usbUp) return Via::Usb;
  if (WiFi.status() == WL_CONNECTED) return Via::Wifi;
  return Via::None;
}

const char *viaName() {
  switch (via()) {
    case Via::Usb:  return "USB";
    case Via::Wifi: return "WiFi";
    default:        return "OFFLINE";
  }
}

bool online() { return via() != Via::None; }

bool get(const String &url, String &out, String &err, uint32_t timeoutMs,
         const char *token) {
  err = "";
  uint32_t timeout = timeoutMs ? timeoutMs : GET_TIMEOUT;

  if (usbUp) {
    int status = 0;
    if (request("GET", url, timeout, out, status)) {
      if (status == 200) return true;
      // The bridge puts its own diagnosis in the body when the fetch failed.
      // That came back promptly, so trying the radio as well costs little and
      // is a real second chance — the bridge's machine may be the thing that
      // is down rather than the link to it.
      err = "USB: " + (out.length() ? out : "HTTP " + String(status));
      out = "";
    } else {
      // A timeout is different, and falling through here used to spend the
      // WiFi timeout straight after the USB one: eighteen seconds in one call,
      // with loop() not running for any of it. request() has already dropped
      // usbUp, so the next poll comes back and takes the radio from the start.
      // One link, one wait.
      err = "USB: no answer";
      return false;
    }
  }

  if (WiFi.status() == WL_CONNECTED) {
    return wifiGet(url, out, err, timeout, token);
  }

  if (err.isEmpty()) err = "no link";
  return false;
}

uint32_t usbSilentFor() {
  if (!everUp || usbUp) return 0;
  uint32_t silent = millis() - lastUsbOk;
  return silent ? silent : 1;  // 0 is reserved for "not silent"
}

bool syncTimeFromHost() {
  if (!usbUp) return false;

  String body;
  int status = 0;
  if (!request("TIME", "", TIME_TIMEOUT, body, status) || status != 200) return false;

  time_t epoch = (time_t)strtoll(body.c_str(), nullptr, 10);
  if (epoch < 1700000000) return false;

  struct timeval tv = {.tv_sec = epoch, .tv_usec = 0};
  settimeofday(&tv, nullptr);
  Serial.printf("[net] clock set from usb host: %ld\n", (long)epoch);
  return true;
}

void setIdleHook(IdleHook fn) { idleHook = fn; }

int readCommand() {
  pump();
  if (cmdTail == cmdHead) return -1;
  uint8_t c = cmdBuf[cmdTail];
  cmdTail = (uint8_t)((cmdTail + 1) % sizeof(cmdBuf));
  return c;
}

}  // namespace net
