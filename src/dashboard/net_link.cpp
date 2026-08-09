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

enum class Rx : uint8_t { Idle, Header, Body };

Rx       rxState = Rx::Idle;
String   rxHeader;
String  *rxSink   = nullptr;  // body destination for the request being awaited
bool     rxKeep   = false;    // false while swallowing a body nobody asked for
int      rxNeed   = 0;
int      rxStatus = 0;
uint16_t rxId     = 0;        // id `rxSink` is waiting for
bool     rxDone   = false;

uint16_t nextId = 1;
uint16_t pingId = 0;

uint32_t lastPing = 0, lastUsbOk = 0;
bool     usbUp = false;

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
  if (sscanf(rxHeader.c_str(), "@RES %u %d %d", &id, &status, &len) != 3) return;
  if (len < 0 || len > MAX_BODY) return;

  if (id == pingId) {
    lastUsbOk = millis();
    usbUp     = true;
    pingId    = 0;
  } else if (rxSink && id == rxId) {
    rxKeep   = true;
    rxStatus = status;
  }

  rxNeed  = len;
  rxState = Rx::Body;
  if (rxNeed == 0) {
    rxState = Rx::Idle;
    if (rxKeep) rxDone = true;
  }
}

void pump() {
  for (;;) {
    if (rxState == Rx::Body) {
      while (rxNeed > 0) {
        uint8_t buf[128];
        int want = rxNeed < (int)sizeof(buf) ? rxNeed : (int)sizeof(buf);
        int n = Serial.readBytes(buf, want);
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
    } else if (c != '\r' && c != '\n') {
      pushCommand((uint8_t)c);
    }
  }
}

// Sends a request and blocks until the matching response lands. `out` is
// cleared first, so a timeout leaves it empty rather than half-filled.
bool request(const char *verb, const String &arg, uint32_t timeout, String &out,
             int &status) {
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
    if (!rxDone) delay(2);
  }

  rxSink = nullptr;
  status = rxStatus;

  if (!rxDone) {
    usbUp = false;
    return false;
  }
  lastUsbOk = millis();
  usbUp     = true;
  return true;
}

// Liveness is asserted without blocking: the ping goes out and the answer is
// picked up by whichever pump() call happens to see it. A blocking probe would
// stall the clock for its whole timeout every time no bridge is running.
void sendPing() {
  pingId = claimId();
  Serial.printf("\n@REQ %u PING\n", pingId);
}

bool wifiGet(const String &url, String &out, String &err) {
  bool secure = url.startsWith("https:");

  WiFiClientSecure tls;
  WiFiClient plain;
  if (secure) {
    tls.setInsecure();  // public endpoints only; no cert pinning to maintain
    tls.setTimeout(10);
  }

  HTTPClient http;
  bool begun = secure ? http.begin(tls, url) : http.begin(plain, url);
  if (!begun) {
    err = "WiFi: connect failed";
    return false;
  }
  http.setTimeout(10000);

  int status = http.GET();
  if (status != HTTP_CODE_OK) {
    err = "WiFi: HTTP " + String(status);
    http.end();
    return false;
  }

  // getString(), not getStream() — Open-Meteo answers with Transfer-Encoding:
  // chunked, and only getString() strips the chunk framing. Feeding the raw
  // stream to ArduinoJson makes it choke on the leading chunk-size line.
  out = http.getString();
  http.end();
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

  // No point pinging a cable that is not plugged into anything: HWCDC drops
  // writes when the host is absent, so the request would never leave.
  if (Serial && millis() - lastPing >= PING_EVERY) {
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

bool get(const String &url, String &out, String &err) {
  err = "";

  if (usbUp) {
    int status = 0;
    if (request("GET", url, GET_TIMEOUT, out, status)) {
      if (status == 200) return true;
      // The bridge puts its own diagnosis in the body when the fetch failed.
      err = "USB: " + (out.length() ? out : "HTTP " + String(status));
      out = "";
    } else {
      err = "USB: no answer";
    }
  }

  if (WiFi.status() == WL_CONNECTED) return wifiGet(url, out, err);

  if (err.isEmpty()) err = "no link";
  return false;
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

int readCommand() {
  pump();
  if (cmdTail == cmdHead) return -1;
  uint8_t c = cmdBuf[cmdTail];
  cmdTail = (uint8_t)((cmdTail + 1) % sizeof(cmdBuf));
  return c;
}

}  // namespace net
