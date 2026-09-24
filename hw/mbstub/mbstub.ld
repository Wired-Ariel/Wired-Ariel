/* mbstub.ld - lo stub "nuovo crt0" che porta il payload dentro Smeraldo.
 *
 * Due regioni, e la distinzione e' il cuore di tutto il blocco W':
 *
 *   ewram  0x02000000..0x0203CFFF  memoria del GIOCO. Lo stub ci gira dentro
 *                                  finche' puo', poi la azzera al posto di
 *                                  RegisterRamReset e sparisce.
 *   tail   0x0203FF00..0x0203FFFF  coda di EWRAM, misurata libera a E-0
 *                                  (0x0203D000-0x02040000, zero scritture del
 *                                  gioco in due sessioni intere). Qui vive
 *                                  handoff.S, che deve SOPRAVVIVERE
 *                                  all'azzeramento perche' e' lui a farlo.
 *
 * La sezione .handoff ha VMA nella coda ma LMA subito dopo .data: altrimenti il
 * linker riempirebbe di zeri i 250 KB fra le due e l'immagine multiboot
 * sforerebbe il limite di 0x3FF40 byte. main.c la copia a mano prima di
 * saltarci dentro.
 *
 * Il payload NON e' una sezione: e' un blob .incbin dentro .rodata, che main.c
 * copia a PAYLOAD_BASE. Cosi' il payload resta un artefatto separato, costruito
 * dalla sua build.ps1, e questo stub non ne conosce il contenuto.
 */

ENTRY(_start)

MEMORY
{
    ewram (rwx) : ORIGIN = 0x02000000, LENGTH = 0x3D000
    tail  (rwx) : ORIGIN = 0x0203FE00, LENGTH = 0x200
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

    /* Il codice che sopravvive all'azzeramento. VMA nella coda, LMA qui. */
    .handoff : {
        . = ALIGN(4);
        __handoff_start = .;
        KEEP(*(.handoff))
        . = ALIGN(4);
        __handoff_end = .;
    } > tail AT> ewram
    __handoff_lma = LOADADDR(.handoff);

    .bss (NOLOAD) : {
        __bss_start = .;
        *(.bss*)
        *(COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > ewram

    /* Stack dello stub, in fondo alla IWRAM. Valgono solo per la fase 1: da
     * handoff.S in poi si usano quelli del gioco (src/crt0.s:28-29). */
    __sp_irq = 0x03007F00;
    __sp_usr = 0x03007E00;

    /DISCARD/ : { *(.comment) *(.ARM.attributes) *(.note*) }
}
