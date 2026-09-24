@ crt0.S - avvio del banco di prova multiboot.
@
@ Un multiboot non e' altro che un binario che il BIOS carica in EWRAM a
@ 0x02000000 e a cui salta. Quindi qui non serve devkitARM ne' libgba: bastano
@ i primi 0xC0 byte di header GBA valido, uno stack e l'azzeramento del BSS.
@
@ I primi 4 byte devono essere un branch: il BIOS ci salta sopra dopo aver
@ verificato il logo Nintendo che sta a 0x04..0x9F. Il logo e il resto
@ dell'header li scrive build.ps1 dopo il link, copiandoli da una ROM vera:
@ qui si lascia lo spazio.

    .section .header, "ax"
    .arm
    .global _vector
_vector:
    b       _start          @ +0x00
    .space  0xBC            @ +0x04 .. +0xBF: logo, titolo, checksum

    .section .text.start, "ax"
    .arm
    .global _start
_start:
    @ Modalita' IRQ: stack suo, che qui non usiamo ma va comunque valido.
    mov     r0, #0x12               @ IRQ mode, IRQ+FIQ disabilitati
    msr     cpsr_c, r0
    ldr     sp, =__sp_irq

    @ Modalita' System: e' dove gira il C.
    mov     r0, #0x1F
    msr     cpsr_c, r0
    ldr     sp, =__sp_usr

    @ Vettore IRQ del BIOS: quando scatta un interrupt, il BIOS salta
    @ all'indirizzo scritto a 0x03007FFC, in ARM. Senza questa riga il driver
    @ SIO e' sordo: SioInit alza il bit in IE, ma nessuno arriva mai a leggere
    @ SIOMULTI0 - ed e' esattamente il difetto che ha tenuto la barra rossa
    @ per una serata intera (2026-08-01).
    ldr     r0, =irq_handler
    ldr     r1, =0x03007FFC
    str     r0, [r1]

    @ BSS a zero. Il C lo da' per scontato, e un contatore che parte da
    @ spazzatura falserebbe ogni misura di questo banco di prova.
    ldr     r0, =__bss_start
    ldr     r1, =__bss_end
    mov     r2, #0
1:  cmp     r0, r1
    strlo   r2, [r0], #4
    blo     1b

    @ In Thumb, come il resto del codice.
    ldr     r0, =main
    bx      r0

    @ Se main tornasse, meglio fermarsi qui che proseguire su memoria a caso.
2:  b       2b

    .pool

    @ Handler IRQ. Il BIOS ha gia' salvato r0-r3, r12 e lr sul suo stack (il
    @ nostro __sp_irq) e arriva qui in modalita' IRQ, stato ARM. L'unico IRQ
    @ abilitato in IE e' il seriale, quindi niente smistamento: si chiama
    @ direttamente il driver, che fa da solo anche l'ACK di REG_IF.
    @ SioOnSerialIrq e' Thumb: serve il giro mov lr,pc + bx (ARMv4T non ha blx).
    .arm
    .global irq_handler
irq_handler:
    stmfd   sp!, {lr}
    ldr     r0, =SioOnSerialIrq
    mov     lr, pc
    bx      r0
    ldmfd   sp!, {lr}
    bx      lr

    .pool
