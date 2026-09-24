/* siotest.ld - il banco di prova vive in EWRAM a 0x02000000.
 *
 * E' l'indirizzo dove il BIOS carica un programma multiboot: non e' una scelta
 * nostra, e' il protocollo. Modellato su payload/payload.ld, ma qui non ci sono
 * contratti di layout da rispettare (niente magic, niente mailbox a offset
 * fisso): il programma e' padrone di tutta la EWRAM.
 *
 * Limite: 0x3FF40 byte, imposto dal caricatore multiboot. build.ps1 lo verifica.
 */

ENTRY(_start)

MEMORY
{
    ewram (rwx) : ORIGIN = 0x02000000, LENGTH = 256K
    iwram (rwx) : ORIGIN = 0x03000000, LENGTH = 32K
}

SECTIONS
{
    .header : {
        KEEP(*(.header))
    } > ewram

    .text : {
        KEEP(*(.text.start))
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    } > ewram

    .data : {
        *(.data*)
        . = ALIGN(4);
    } > ewram

    .bss (NOLOAD) : {
        __bss_start = .;
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > ewram

    /* Gli stack stanno in fondo alla IWRAM, come fa ogni programma GBA: e' la
     * memoria veloce, e non toglie spazio al binario che va trasferito via
     * cavo. */
    __sp_irq = ORIGIN(iwram) + LENGTH(iwram) - 0x100;
    __sp_usr = ORIGIN(iwram) + LENGTH(iwram) - 0x200;

    /DISCARD/ : { *(.comment) *(.ARM.attributes) *(.note*) }
}
