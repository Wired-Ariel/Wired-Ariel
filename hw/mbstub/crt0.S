@ crt0.S - avvio dello stub multiboot (fase 1).
@
@ Modellato su hw/siotest/crt0.S, che e' gia' passato sull'hardware (M-3, la
@ schermata del test compare). Qui pero' non serve nessun handler IRQ: la fase 1
@ non usa interrupt, e dalla fase 3 in poi il vettore e' quello del gioco.
@
@ I primi 0xC0 byte sono l'header GBA. Il BIOS verifica il logo Nintendo a
@ 0x04..0x9F prima di accettare l'immagine: lo scrive build.ps1 dopo il link,
@ copiandolo da una ROM vera. Qui si lascia solo lo spazio.

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
    mov     r0, #0x12
    msr     cpsr_c, r0
    ldr     sp, =__sp_irq

    @ Modalita' System: e' dove gira il C.
    mov     r0, #0x1F
    msr     cpsr_c, r0
    ldr     sp, =__sp_usr

    @ BSS a zero: il C lo da' per scontato.
    ldr     r0, =__bss_start
    ldr     r1, =__bss_end
    mov     r2, #0
1:  cmp     r0, r1
    strlo   r2, [r0], #4
    blo     1b

    @ In Thumb, come il resto del codice.
    ldr     r0, =main
    bx      r0

    @ main non torna mai: salta dentro AgbMain e da li' il gioco non rende piu'
    @ il controllo. Se tornasse, meglio fermarsi che proseguire su memoria a caso.
2:  b       2b

    .pool
