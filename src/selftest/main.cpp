// ---------------------------------------------------------------------------
//  LilyGO T-Display-S3 — display self-test
//
//  ESP32-S3 + ST7789 170x320, 8-bit parallel (i8080) bus, driven in landscape.
//  Runs a loop of test patterns; progress is also printed to the USB serial
//  monitor so you can tell a hung sketch apart from a dark panel.
// ---------------------------------------------------------------------------

#include <Arduino.h>
#include <TFT_eSPI.h>

// The LCD rail is gated behind GPIO15 — without this the panel never lights up.
#define PIN_POWER_ON 15
#define PIN_BUTTON_1 0   // BOOT
#define PIN_BUTTON_2 14

TFT_eSPI tft = TFT_eSPI();

static const uint8_t  ROT = 1;    // landscape, USB on the right
static const uint16_t W = 320;
static const uint16_t H = 170;

// --- helpers ---------------------------------------------------------------

static void banner(const char *title) {
  Serial.printf("[test] %s\n", title);
  tft.setRotation(ROT);
  tft.fillScreen(TFT_BLACK);
  tft.setTextDatum(TC_DATUM);
  tft.setTextColor(TFT_YELLOW, TFT_BLACK);
  tft.drawString(title, W / 2, 4, 2);
}

// --- 1. backlight ----------------------------------------------------------
// Ramps the LED backlight with PWM. If the screen stays black here but the
// serial log advances, the problem is power/backlight, not the parallel bus.
static void testBacklight() {
  Serial.println("[test] backlight fade");
  tft.fillScreen(TFT_WHITE);
  tft.setTextColor(TFT_BLACK, TFT_WHITE);
  tft.setTextDatum(MC_DATUM);
  tft.drawString("BACKLIGHT", W / 2, H / 2, 4);

  for (int duty = 255; duty >= 20; duty -= 5) { analogWrite(TFT_BL, duty); delay(8); }
  for (int duty = 20; duty <= 255; duty += 5) { analogWrite(TFT_BL, duty); delay(8); }
  analogWrite(TFT_BL, 255);
  delay(300);
}

// --- 2. solid fills --------------------------------------------------------
// Full-screen primaries: catches dead pixels, and a swapped red/blue here means
// TFT_RGB_ORDER in platformio.ini needs flipping to TFT_BGR.
static void testSolidFills() {
  struct { uint16_t color; const char *name; uint16_t label; } fills[] = {
    {TFT_RED,   "RED",   TFT_WHITE},
    {TFT_GREEN, "GREEN", TFT_BLACK},
    {TFT_BLUE,  "BLUE",  TFT_WHITE},
    {TFT_WHITE, "WHITE", TFT_BLACK},
    {TFT_BLACK, "BLACK", TFT_WHITE},
  };

  tft.setRotation(ROT);
  tft.setTextDatum(MC_DATUM);
  for (auto &f : fills) {
    Serial.printf("[test] fill %s\n", f.name);
    uint32_t t0 = millis();
    tft.fillScreen(f.color);
    uint32_t dt = millis() - t0;
    tft.setTextColor(f.label, f.color);
    tft.drawString(f.name, W / 2, H / 2 - 12, 4);
    tft.drawString(String(dt) + " ms", W / 2, H / 2 + 18, 2);
    delay(700);
  }
}

// --- 3. colour gradient ----------------------------------------------------
// 16-bit RGB565 ramps. Banding or missing steps points at a flaky data line.
static void testGradient() {
  banner("GRADIENT");
  const int top = 26;
  const int barH = (H - top - 8) / 3;

  for (int x = 0; x < W; x++) {
    uint8_t v = map(x, 0, W - 1, 0, 255);
    tft.drawFastVLine(x, top,            barH, tft.color565(v, 0, 0));
    tft.drawFastVLine(x, top + barH,     barH, tft.color565(0, v, 0));
    tft.drawFastVLine(x, top + barH * 2, barH, tft.color565(0, 0, v));
  }
  delay(1500);
}

// --- 4. geometry -----------------------------------------------------------
static void testGeometry() {
  banner("GEOMETRY");

  // 1px border — if an edge is clipped, the CGRAM offset is wrong.
  tft.drawRect(0, 0, W, H, TFT_WHITE);

  for (int i = 0; i < 12; i++) {
    tft.drawLine(0, 26 + i * 6, W - 1, H - 1 - i * 6,
                 tft.color565(255 - i * 20, i * 20, 128));
  }
  for (int r = 10; r < 70; r += 10) {
    tft.drawCircle(W / 2, H / 2, r, TFT_CYAN);
  }
  tft.fillCircle(W / 2, H / 2, 8, TFT_MAGENTA);
  tft.fillTriangle(W - 90, H - 10, W - 50, H - 60, W - 10, H - 10, TFT_ORANGE);
  tft.fillRoundRect(14, 34, 60, 40, 8, TFT_GREENYELLOW);
  delay(1800);
}

// --- 5. fonts --------------------------------------------------------------
// Two columns, because landscape has width to spare and only 170px of height.
static void testFonts() {
  banner("FONTS");
  tft.setTextDatum(TL_DATUM);

  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("Font 2 abcABC 123", 6, 30, 2);
  tft.drawString("Font 4 abcABC", 6, 52, 4);
  tft.setTextColor(TFT_GREEN, TFT_BLACK);
  tft.drawString("123456", 6, 84, 6);
  tft.setTextColor(TFT_ORANGE, TFT_BLACK);
  tft.drawString("88:88", 6, 124, 7);

  tft.setTextColor(TFT_CYAN, TFT_BLACK);
  tft.drawString("2468", 170, 60, 8);

  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.setTextDatum(BR_DATUM);
  tft.drawString("320 x 170 landscape", W - 6, H - 4, 2);
  delay(2000);
}

// --- 6. rotation -----------------------------------------------------------
// Each orientation should read upright with the marker box in the top-left.
static void testRotation() {
  Serial.println("[test] rotation");
  for (uint8_t r = 0; r < 4; r++) {
    tft.setRotation(r);
    tft.fillScreen(TFT_NAVY);
    tft.fillRect(0, 0, 20, 20, TFT_RED);          // top-left marker
    tft.drawRect(0, 0, tft.width(), tft.height(), TFT_WHITE);
    tft.setTextDatum(MC_DATUM);
    tft.setTextColor(TFT_WHITE, TFT_NAVY);
    tft.drawString("ROT " + String(r), tft.width() / 2, tft.height() / 2 - 12, 4);
    tft.drawString(String(tft.width()) + "x" + String(tft.height()),
                   tft.width() / 2, tft.height() / 2 + 16, 2);
    delay(900);
  }
  tft.setRotation(ROT);
}

// --- 7. throughput ---------------------------------------------------------
// Full-screen sprite pushed as fast as possible. Expect roughly 30-40 FPS;
// far below that means the parallel bus fell back to a slow write path.
static void testFrameRate() {
  Serial.println("[test] frame rate");
  TFT_eSprite spr = TFT_eSprite(&tft);
  spr.setColorDepth(16);

  if (!spr.createSprite(W, H)) {
    Serial.println("[test] sprite alloc failed - skipping FPS test");
    banner("FPS: no memory");
    delay(1500);
    return;
  }

  float x = W / 2, y = H / 2, dx = 3.1f, dy = 2.3f;
  const int radius = 14;
  uint32_t frames = 0, t0 = millis();

  while (millis() - t0 < 4000) {
    x += dx; y += dy;
    if (x < radius || x > W - radius) dx = -dx;
    if (y < radius || y > H - radius) dy = -dy;

    spr.fillSprite(TFT_BLACK);
    spr.drawRect(0, 0, W, H, TFT_DARKGREY);
    spr.fillCircle((int)x, (int)y, radius, TFT_YELLOW);
    spr.setTextColor(TFT_WHITE, TFT_BLACK);
    spr.drawString("FPS TEST", 8, 8, 2);
    spr.pushSprite(0, 0);
    frames++;
  }

  float fps = frames * 1000.0f / (millis() - t0);
  spr.deleteSprite();

  Serial.printf("[test] %.1f fps over %lu frames\n", fps, (unsigned long)frames);
  banner("FRAME RATE");
  tft.setTextDatum(MC_DATUM);
  tft.setTextColor(TFT_GREEN, TFT_BLACK);
  tft.drawString(String(fps, 1), W / 2, H / 2 - 10, 6);
  tft.setTextColor(TFT_WHITE, TFT_BLACK);
  tft.drawString("frames / sec", W / 2, H / 2 + 30, 4);
  delay(2000);
}

// --- 8. board info ---------------------------------------------------------
static void showBoardInfo(uint32_t pass) {
  banner("T-DISPLAY-S3");
  tft.setTextColor(TFT_WHITE, TFT_BLACK);

  int16_t y = 30;
  auto row = [&](int16_t x, const String &k, const String &v) {
    tft.setTextDatum(TL_DATUM);
    tft.setTextColor(TFT_DARKGREY, TFT_BLACK); tft.drawString(k, x, y, 2);
    tft.setTextColor(TFT_WHITE, TFT_BLACK);    tft.drawString(v, x + 72, y, 2);
  };

  row(8,   "chip",  ESP.getChipModel());
  row(168, "panel", "ST7789");
  y += 20;
  row(8,   "cores", String(ESP.getChipCores()));
  row(168, "size",  "320x170");
  y += 20;
  row(8,   "cpu",   String(getCpuFrequencyMhz()) + " MHz");
  row(168, "bus",   "8-bit par.");
  y += 20;
  row(8,   "flash", String(ESP.getFlashChipSize() / (1024 * 1024)) + " MB");
  row(168, "heap",  String(ESP.getFreeHeap() / 1024) + " KB");
  y += 20;
  row(8,   "psram", String(ESP.getPsramSize() / (1024 * 1024)) + " MB");
  row(168, "pass",  String(pass));

  delay(2500);
}

// ---------------------------------------------------------------------------

void setup() {
  // Hold the LCD power rail up before touching the panel.
  pinMode(PIN_POWER_ON, OUTPUT);
  digitalWrite(PIN_POWER_ON, HIGH);

  pinMode(PIN_BUTTON_1, INPUT_PULLUP);
  pinMode(PIN_BUTTON_2, INPUT_PULLUP);

  Serial.begin(115200);
  delay(300);  // let the USB CDC port enumerate before the first print
  Serial.println("\n=== T-Display-S3 display self-test ===");

  tft.init();
  tft.setRotation(ROT);
  tft.fillScreen(TFT_BLACK);

  pinMode(TFT_BL, OUTPUT);
  digitalWrite(TFT_BL, HIGH);

  Serial.printf("chip=%s cores=%d flash=%luMB psram=%luMB\n",
                ESP.getChipModel(), ESP.getChipCores(),
                (unsigned long)(ESP.getFlashChipSize() / (1024 * 1024)),
                (unsigned long)(ESP.getPsramSize() / (1024 * 1024)));
  Serial.printf("panel=%dx%d\n", tft.width(), tft.height());
}

void loop() {
  static uint32_t pass = 0;
  pass++;

  showBoardInfo(pass);
  testBacklight();
  testSolidFills();
  testGradient();
  testGeometry();
  testFonts();
  testRotation();
  testFrameRate();

  Serial.printf("[test] pass %lu complete\n", (unsigned long)pass);
}
