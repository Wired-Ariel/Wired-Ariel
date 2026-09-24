/* 
 * The MIT License (MIT)
 *
 * Copyright (c) 2019 Ha Thach (tinyusb.org)
 *
 * Permission is hereby granted, free of charge, to any person obtaining a copy
 * of this software and associated documentation files (the "Software"), to deal
 * in the Software without restriction, including without limitation the rights
 * to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
 * copies of the Software, and to permit persons to whom the Software is
 * furnished to do so, subject to the following conditions:
 *
 * The above copyright notice and this permission notice shall be included in
 * all copies or substantial portions of the Software.
 *
 * THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
 * IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
 * FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
 * AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
 * LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
 * OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
 * THE SOFTWARE.
 *
 */

#include <stdlib.h>
#include <stdio.h>
#include <string.h>

#include "bsp/board.h"
#include "tusb.h"
#include "usb_descriptors.h"
#include <stdio.h>
#include <string.h>
#include "hardware/pio.h"
#include "pio/pio_spi.h"
#include "pico/time.h"

#define NUM_CMP_BYTES 0x20
#define NUM_CMP_BYTES_RECV (NUM_CMP_BYTES+4)

/* Pacchetto di configurazione ESTESO: stesso magic di 32 byte, ma 8 byte di
 * coda invece di 4, cosi' porta anche i tre pin. La lunghezza 40 e' scelta
 * perche' non collide con niente di quello che manda multiboot.py, che usa
 * solo pacchetti da 4 byte (una parola) e da 64 (un blocco di payload).
 * Il pacchetto da 36 resta identico: la compatibilita' con l'upstream non si
 * tocca, questo e' un comando in piu'. */
#define NUM_PINCFG_BYTES_RECV  (NUM_CMP_BYTES+8)
#define PINCFG_CMD_SET_PINS    0x01
#define PINCFG_CMD_READ_LEVELS 0x02
#define PINCFG_REPLY_PINS      0xA1
#define PINCFG_REPLY_LEVELS    0xA2

/* GP0..GP5: tutte le linee che la game-boy-pico-link-board porta al
 * connettore link (GP0=SC, GP1=SI, GP2=SO, GP3=SD secondo lo schematico;
 * GP4/GP5 restano candidati perche' altre board le usano per SD). */
#define LINK_PIN_COUNT 6

#define NUM_DEFAULT_BYTES_PER_TRANSFER 1
#define US_DEFAULT_PER_TRANSFER 1000

#define MAX_TRANSFER_BYTES 0x40

#define PIN_SCK 0
#define PIN_SIN 1
#define TEST_PIN 6

uint PIN_SOUT = 2;
uint SI_PIN = 3;

/* Pin realmente in uso adesso, e offset del programma PIO: servono per poter
 * rifare pio_spi_init() con un'assegnazione diversa senza ricaricare il
 * programma nella PIO. */
static uint g_prog_offs = 0;
static uint g_pin_sck  = PIN_SCK;
static uint g_pin_mosi = 2;
static uint g_pin_miso = 3;

/* Divisore di clock della PIO. Il valore dell'upstream (4058.838/128 = 31.7)
 * da' circa 1 MHz di SCK, e su questa board il segnale passa da un BOB-12009,
 * che e' uno shifter a MOSFET con pull-up da 10 kOhm: sopra qualche centinaio
 * di kHz i fronti di salita si arrotondano parecchio. Poterlo rallentare
 * separa "pin sbagliati" da "troppo veloce per lo shifter", che altrimenti
 * danno lo stesso sintomo. Preset 0..3 = base x1, x4, x16, x64. */
#define LINK_CLKDIV_BASE (4058.838f/128)
#define LINK_CLOCK_PRESETS 4
static float g_clkdiv = LINK_CLKDIV_BASE;
static uint8_t g_clock_preset = 0;

bool is_test_pin_grounded() {
  gpio_init(TEST_PIN);
  gpio_set_dir(TEST_PIN, GPIO_OUT);
  gpio_put(TEST_PIN, 1);  // Set the pin high

  // Read the state of the pin
  bool grounded = gpio_get(TEST_PIN) == 0;

  return grounded;
}

//--------------------------------------------------------------------+
// MACRO CONSTANT TYPEDEF PROTYPES
//--------------------------------------------------------------------+

/* Blink pattern
 * - 250 ms  : device not mounted
 * - 1000 ms : device mounted
 * - 2500 ms : device is suspended
 */
enum  {
  BLINK_NOT_MOUNTED = 250,
  BLINK_MOUNTED     = 1000,
  BLINK_SUSPENDED   = 2500,

  BLINK_ALWAYS_ON   = UINT32_MAX,
  BLINK_ALWAYS_OFF  = 0
};

static uint32_t blink_interval_ms = BLINK_NOT_MOUNTED;
static uint8_t data_buf[MAX_TRANSFER_BYTES];
static uint8_t compare_bytes[NUM_CMP_BYTES] = {0xCA, 0xFE, 0xCA, 0xFE, 0xCA, 0xFE, 0xCA, 0xFE, 0xCA, 0xFE, 0xCA, 0xFE, 0xCA, 0xFE, 0xCA, 0xFE, 0xDE, 0xAD, 0xBE, 0xEF, 0xDE, 0xAD, 0xBE, 0xEF, 0xDE, 0xAD, 0xBE, 0xEF, 0xDE, 0xAD, 0xBE, 0xEF};
static uint8_t buf_count;
static uint8_t num_bytes_per_transfer = NUM_DEFAULT_BYTES_PER_TRANSFER;
static uint32_t us_between_transfer = US_DEFAULT_PER_TRANSFER;
static uint32_t total_transferred = 0;

#define URL  "tetris.gblink.io"

const tusb_desc_webusb_url_t desc_url =
{
  .bLength         = 3 + sizeof(URL) - 1,
  .bDescriptorType = 3, // WEBUSB URL type
  .bScheme         = 1, // 0: http, 1: https
  .url             = URL
};

static bool web_serial_connected = false;

//------------- prototypes -------------//
void handle_input_data(uint8_t* buf_in, uint32_t count);
void data_transfer_task(void);
void led_blinking_task(void);
void cdc_task(void);
void webserial_task(void);

/*------------- MAIN -------------*/

  pio_spi_inst_t spi = {
          .pio = pio1,
          .sm = 0
  };

/* Riporta un pin a GPIO d'ingresso neutro. Va fatto sui pin che il PIO stava
 * pilotando prima di cambiare assegnazione: se restassero pilotati da noi, la
 * combinazione successiva misurerebbe una linea tenuta dalla board, non dal
 * GBA - cioe' darebbe un falso positivo. */
static void link_release_pin(uint p)
{
    gpio_set_outover(p, GPIO_OVERRIDE_NORMAL);
    gpio_set_function(p, GPIO_FUNC_SIO);
    gpio_set_dir(p, GPIO_IN);
    gpio_disable_pulls(p);
}

static void link_apply_pins(uint sck, uint mosi, uint miso)
{
    pio_sm_set_enabled(spi.pio, spi.sm, false);
    pio_sm_clear_fifos(spi.pio, spi.sm);

    for (uint p = 0; p < LINK_PIN_COUNT; p++)
        link_release_pin(p);

    pio_spi_init(spi.pio, spi.sm, g_prog_offs, 8, g_clkdiv, 1, 1,
                 sck, mosi, miso);

    g_pin_sck  = sck;
    g_pin_mosi = mosi;
    g_pin_miso = miso;
}

/* Legge il livello di GP0..GP5 una volta col pull-up e una col pull-down.
 * Le due letture insieme distinguono i tre casi che ci interessano:
 *   up=1 down=0 -> linea LIBERA (nessuno la pilota)
 *   up=0 down=0 -> linea tenuta a MASSA (dal cavo o da chi sta dall'altra parte)
 *   up=1 down=1 -> linea tenuta ALTA
 * E' la misura che decide se il cavo GBA cortocircuita davvero SI a massa. */
static uint8_t link_sample_levels(bool pull_up)
{
    uint8_t levels = 0;

    for (uint p = 0; p < LINK_PIN_COUNT; p++) {
        gpio_set_outover(p, GPIO_OVERRIDE_NORMAL);
        gpio_set_function(p, GPIO_FUNC_SIO);
        gpio_set_dir(p, GPIO_IN);
        if (pull_up)
            gpio_pull_up(p);
        else
            gpio_pull_down(p);
    }

    busy_wait_us(500);

    for (uint p = 0; p < LINK_PIN_COUNT; p++)
        if (gpio_get(p))
            levels |= (uint8_t)(1u << p);

    for (uint p = 0; p < LINK_PIN_COUNT; p++)
        gpio_disable_pulls(p);

    return levels;
}


int main(void)
{
  // Check the state of TEST_PIN
  if (is_test_pin_grounded()) {
    // GPIO 6 (TEST_PIN) is grounded, update PIN_SOUT and SI_PIN
    PIN_SOUT = 3;
    SI_PIN = 4;
  }
  else {
    // GPIO 6 (TEST_PIN) is not grounded, use default values
    PIN_SOUT = 2;
    SI_PIN = 3;
  }

  //board_init();
  buf_count = 0;
  g_prog_offs = pio_add_program(spi.pio, &spi_cpha1_program);

  /* Ingresso su PIN_SIN (GP1), che e' il valore dell'upstream e quello con cui
   * il multiboot ha storicamente funzionato. La copia in D:\Progettini\trades
   * era stata ricompilata con l'ingresso su SI_PIN (GP3 = SD): in SIO Normal
   * 32 bit - la modalita' del multiboot - il GBA risponde sul proprio SO, che
   * su SD non passa mai, quindi con quella build l'handshake non puo' tornare.
   * Comunque non e' un valore su cui fidarsi: il PC puo' cambiarlo a runtime
   * col comando PINCFG e provare le combinazioni finche' una risponde. */
  link_apply_pins(PIN_SCK, PIN_SOUT, PIN_SIN);

  tusb_init();

  while (1)
  {
    tud_task(); // tinyusb device task
    data_transfer_task();
    cdc_task();
    webserial_task();
    led_blinking_task();
  }

  return 0;
}

int oldmain(void)
{
  board_init();

  tusb_init();

  while (1)
  {
    tud_task(); // tinyusb device task
    cdc_task();
    webserial_task();
    led_blinking_task();
  }

  return 0;
}

// send characters to both CDC and WebUSB
void echo_all(uint8_t buf[], uint32_t count)
{
  // echo to web serial
  if ( web_serial_connected )
  {
    tud_vendor_write(buf, count);
    tud_vendor_flush();
  }

  // echo to cdc
  if ( tud_cdc_connected() )
  {
    for(uint32_t i=0; i<count; i++)
    {
      tud_cdc_write_char(buf[i]);
    }
    tud_cdc_write_flush();
  }
}

//--------------------------------------------------------------------+
// Device callbacks
//--------------------------------------------------------------------+

// Invoked when device is mounted
void tud_mount_cb(void)
{
  blink_interval_ms = BLINK_MOUNTED;
}

// Invoked when device is unmounted
void tud_umount_cb(void)
{
  blink_interval_ms = BLINK_NOT_MOUNTED;
}

// Invoked when usb bus is suspended
// remote_wakeup_en : if host allow us  to perform remote wakeup
// Within 7ms, device must draw an average of current less than 2.5 mA from bus
void tud_suspend_cb(bool remote_wakeup_en)
{
  (void) remote_wakeup_en;
  blink_interval_ms = BLINK_SUSPENDED;
}

// Invoked when usb bus is resumed
void tud_resume_cb(void)
{
  blink_interval_ms = BLINK_MOUNTED;
}

//--------------------------------------------------------------------+
// WebUSB use vendor class
//--------------------------------------------------------------------+

// Invoked when a control transfer occurred on an interface of this class
// Driver response accordingly to the request and the transfer stage (setup/data/ack)
// return false to stall control endpoint (e.g unsupported request)
bool tud_vendor_control_xfer_cb(uint8_t rhport, uint8_t stage, tusb_control_request_t const * request)
{
  // nothing to do for DATA & ACK stage
  if (stage != CONTROL_STAGE_SETUP) return true;

  switch (request->bRequest)
  {
    case VENDOR_REQUEST_WEBUSB:
      // match vendor request in BOS descriptor
      // Get landing page url
      return tud_control_xfer(rhport, request, (void*) &desc_url, desc_url.bLength);

    case VENDOR_REQUEST_MICROSOFT:
      if ( request->wIndex == 7 )
      {
        // Get Microsoft OS 2.0 compatible descriptor
        uint16_t total_len;
        memcpy(&total_len, desc_ms_os_20+8, 2);

        return tud_control_xfer(rhport, request, (void*) desc_ms_os_20, total_len);
      }else
      {
        return false;
      }
    case 0x22:
      // Webserial simulate the CDC_REQUEST_SET_CONTROL_LINE_STATE (0x22) to connect and disconnect.
      web_serial_connected = (request->wValue != 0);
      
      total_transferred = 0;
      num_bytes_per_transfer = NUM_DEFAULT_BYTES_PER_TRANSFER;
      us_between_transfer = US_DEFAULT_PER_TRANSFER;

      // Always lit LED if connected
      if ( web_serial_connected )
      {
        board_led_write(true);
        blink_interval_ms = BLINK_ALWAYS_ON;

        // tud_vendor_write_str("\r\nTinyUSB WebUSB device example\r\n");
      }else
      {
        blink_interval_ms = BLINK_MOUNTED;
      }

      // response with status OK
      return tud_control_status(rhport, request);
      break;

    default: break;
  }

  // stall unknown request
  return false;
}

// Invoked when DATA Stage of VENDOR's request is complete
bool tud_vendor_control_complete_cb(uint8_t rhport, tusb_control_request_t const * request)
{
  (void) rhport;
  (void) request;

  // nothing to do
  return true;
}

void data_transfer_task(void) {
    //if(buf_count) {
        //uint8_t buf_out[MAX_TRANSFER_BYTES];
    //    for(int i = 0; i < (buf_count+3) >> 2; i++) {
    //        pio_spi_write8_blocking(&spi, data_buf+(4*i), 4);
    //        busy_wait_us(36);
    //    }
        //pio_spi_write8_read8_blocking(&spi, data_buf, buf_out, buf_count);
        //echo_all(buf_out, buf_count);
    //    buf_count = 0;
    //}
}

void handle_input_data(uint8_t* buf_in, uint32_t count) {
  for(int i = count; i < (MAX_TRANSFER_BYTES*2); i++)
    buf_in[i] = 0;
  uint8_t processed = 0;
  if(count == NUM_CMP_BYTES_RECV) {
    uint8_t failed = 0;
    for(int i = 0; i < NUM_CMP_BYTES; i++)
      if(buf_in[i] != compare_bytes[i]) {
        failed = 1;
        break;
      }
    if(!failed) {
      us_between_transfer = (buf_in[NUM_CMP_BYTES]<<0) + (buf_in[NUM_CMP_BYTES+1]<<8) + (buf_in[NUM_CMP_BYTES+2]<<16);
      num_bytes_per_transfer = buf_in[NUM_CMP_BYTES+3];
      if(num_bytes_per_transfer > MAX_TRANSFER_BYTES)
        num_bytes_per_transfer = MAX_TRANSFER_BYTES;
      processed = 1;
      echo_all(&processed, 1);
    }
  }
  if(!processed && count == NUM_PINCFG_BYTES_RECV) {
    uint8_t failed = 0;
    for(int i = 0; i < NUM_CMP_BYTES; i++)
      if(buf_in[i] != compare_bytes[i]) {
        failed = 1;
        break;
      }
    if(!failed) {
      us_between_transfer = (buf_in[NUM_CMP_BYTES]<<0) + (buf_in[NUM_CMP_BYTES+1]<<8) + (buf_in[NUM_CMP_BYTES+2]<<16);
      num_bytes_per_transfer = buf_in[NUM_CMP_BYTES+3];
      if(num_bytes_per_transfer > MAX_TRANSFER_BYTES)
        num_bytes_per_transfer = MAX_TRANSFER_BYTES;

      uint8_t sck  = buf_in[NUM_CMP_BYTES+4];
      uint8_t mosi = buf_in[NUM_CMP_BYTES+5];
      uint8_t miso = buf_in[NUM_CMP_BYTES+6];
      uint8_t raw  = buf_in[NUM_CMP_BYTES+7];
      uint8_t cmd    = raw & 0x0F;
      uint8_t preset = raw >> 4;

      if(preset < LINK_CLOCK_PRESETS) {
        g_clock_preset = preset;
        g_clkdiv = LINK_CLKDIV_BASE * (float)(1u << (2 * preset));
      }

      if(cmd == PINCFG_CMD_READ_LEVELS) {
        uint8_t up   = link_sample_levels(true);
        uint8_t down = link_sample_levels(false);
        /* La misura ha staccato i pin dal PIO: rimetterli com'erano, altrimenti
         * il trasferimento successivo parte su una configurazione morta. */
        link_apply_pins(g_pin_sck, g_pin_mosi, g_pin_miso);
        uint8_t reply[4] = { PINCFG_REPLY_LEVELS, up, down, 0 };
        echo_all(reply, 4);
      }
      else {
        if(sck < LINK_PIN_COUNT && mosi < LINK_PIN_COUNT && miso < LINK_PIN_COUNT
           && sck != mosi && sck != miso && mosi != miso)
          link_apply_pins(sck, mosi, miso);
        /* Si risponde con i pin REALMENTE in uso, non con quelli chiesti: se la
         * richiesta era assurda il PC lo vede, invece di credere di aver
         * cambiato qualcosa. */
        uint8_t reply[5] = { PINCFG_REPLY_PINS,
                             (uint8_t)g_pin_sck, (uint8_t)g_pin_mosi, (uint8_t)g_pin_miso,
                             g_clock_preset };
        echo_all(reply, 5);
      }
      processed = 1;
    }
  }
  if(!processed) {
    // pprintf("Sending: %02x", buf[0]);
    uint8_t total_processed = 0;
    uint8_t buf_out[MAX_TRANSFER_BYTES*2];
    while(total_processed < count) {
      uint8_t transferable = num_bytes_per_transfer;
      //if(count-total_processed < transferable)
        //transferable = count-total_processed;
      pio_spi_write8_read8_blocking(&spi, buf_in + total_processed, buf_out + total_processed, transferable);
      total_transferred += transferable;
      total_processed += transferable;
      busy_wait_us(us_between_transfer);
    }
    echo_all(buf_out, total_processed);
    //echo_all(&availables, 1);
  }
}

void webserial_task(void)
{
  if ( web_serial_connected )
    if ( tud_vendor_available() ) {
      uint8_t buf_in[MAX_TRANSFER_BYTES*2];
      uint32_t count = tud_vendor_read(buf_in, sizeof(buf_in));
      handle_input_data(buf_in, count);
    }
}


//--------------------------------------------------------------------+
// USB CDC
//--------------------------------------------------------------------+
void cdc_task(void)
{
  if ( tud_cdc_connected() )
    // connected and there are data available
    if ( tud_cdc_available() ) {
      uint8_t buf_in[MAX_TRANSFER_BYTES*2];
      uint32_t count = tud_cdc_read((uint8_t*)buf_in, sizeof(buf_in));
      handle_input_data(buf_in, count);
    }
}

// Invoked when cdc when line state changed e.g connected/disconnected
void tud_cdc_line_state_cb(uint8_t itf, bool dtr, bool rts)
{
  (void) itf;

  // connected
  if ( dtr && rts )
  {
    // print initial message when connected
    // tud_cdc_write_str("\r\nTinyUSB WebUSB device example\r\n");
  }
}

// Invoked when CDC interface received data from host
void tud_cdc_rx_cb(uint8_t itf)
{
  (void) itf;
}

//--------------------------------------------------------------------+
// BLINKING TASK
//--------------------------------------------------------------------+
void led_blinking_task(void)
{
  static uint32_t start_ms = 0;
  static bool led_state = false;

  // Blink every interval ms
  if ( board_millis() - start_ms < blink_interval_ms) return; // not enough time
  start_ms += blink_interval_ms;

  board_led_write(led_state);
  led_state = 1 - led_state; // toggle
}
