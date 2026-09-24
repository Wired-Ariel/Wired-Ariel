@ =============================================================================
@ hook.S — punto di ingresso del payload, agganciato al vettore IRQ 0x03007FFC
@ =============================================================================
@
@ Strategia (vedi CLAUDE.md, decisione architetturale n.3):
@ il vettore IRQ in IWRAM persiste ai cambi di scena, mentre gMain.vblankCallback
@ viene riscritto di continuo. Ci agganciamo lì e concateniamo all'handler originale.
@
@ Ordine scelto: **il nostro codice gira PRIMA**, poi si salta all'handler originale
@ con un tail jump. Così non dipendiamo dal fatto che l'handler del gioco preservi
@ il nostro lr — l'originale ritorna direttamente al BIOS.
@
@ Il BIOS salva r0-r3, r12 e lr prima di chiamare l'handler, quindi possiamo
@ sporcarli liberamente.
@
@ Layout dei primi 12 byte (l'iniettore ci scrive dentro):
@   +0x00  b hook_entry        <- il vettore IRQ punta qui
@   +0x04  g_origHandler       <- riempito dall'iniettore col vecchio vettore
@   +0x08  g_magic             <- 'HOOK', per verificare che il payload sia vivo

    .section .text.start, "ax"
    .arm
    .global _start
_start:
    b       hook_entry

    .global g_origHandler
g_origHandler:
    .word   0

    .global g_magic
g_magic:
    .word   0x4B4F4F48          @ "HOOK" in little endian

    .text
    .arm
    .global hook_entry
    .type   hook_entry, %function
hook_entry:
    stmfd   sp!, {lr}           @ lr del BIOS: ci serve intatto per il tail jump

    @ --- passaggio al nostro stack -------------------------------------------
    @ Lo stack IRQ del GBA è minuscolo (~0x60 byte a 0x03007FA0): non ci si può
    @ chiamare dentro codice C. Usiamo uno stack nostro dentro il payload.
    ldr     r0, =payload_stack_top
    mov     r1, sp
    mov     sp, r0
    stmfd   sp!, {r1, lr}

    ldr     r0, =payload_frame
    mov     lr, pc
    bx      r0                  @ payload_frame() è Thumb: bx gestisce l'interworking

    @ --- LA FALLA CHIUSA (2026-08-16) ----------------------------------------
    @ Qui sotto c'era il difetto che ha prodotto il blocco a intermittenza del
    @ gioco, e non era nei registri: era in QUESTO ORDINE.
    @
    @ Mentre payload_frame() gira, gli IRQ sono spenti a livello di CPU ma il
    @ bit 7 di REG_IF si ALZA lo stesso quando arriva uno scambio seriale - e
    @ ne arriva uno ogni ~4,4 ms, cioè quasi certamente dentro un nostro giro
    @ di VBlank. Al tail jump qui sotto, IntrMain legge `IF & IE`, ci trova il
    @ bit seriale, e chiama SerialIntr -> SerialCB del gioco: la macchina di
    @ link del gioco si SVEGLIA con dati che non sono suoi. Da lì
    @ CheckMasterOrSlave lo convince di essere master, InitTimer arma il
    @ Timer 3 (nel log: IE passa da 0x0085 a 0x00C5 e non torna più indietro),
    @ StartTransfer comincia a riscrivergli SIOCNT addosso a ogni frame - le
    @ nostre RIPARAZIONI - e il gioco si mette ad aspettare un partner che non
    @ parla la sua lingua. Il resto - lo scatto, l'"anche uscendo prosegue" -
    @ è conseguenza.
    @
    @ La cura è raccogliere ciò che è caduto nella nostra finestra PRIMA di
    @ passare la mano: payload_drain() gestisce e riconosce l'IRQ seriale
    @ rimasto in sospeso, così IntrMain non lo vede più. Se la porta non è
    @ nostra (Cable Club) payload_drain() non tocca niente e l'IRQ va al
    @ gioco, che è esattamente ciò che deve succedere lì.
    ldr     r0, =payload_drain
    mov     lr, pc
    bx      r0

    ldmfd   sp!, {r1, lr}
    mov     sp, r1              @ ripristina lo stack IRQ
    ldmfd   sp!, {lr}           @ ripristina il lr del BIOS

    @ --- tail jump all'handler originale --------------------------------------
    ldr     r12, =g_origHandler
    ldr     r12, [r12]
    cmp     r12, #0
    bxne    r12                 @ l'originale ritornerà al BIOS con la lr giusta
    bx      lr                  @ nessun handler originale: torniamo noi
    .size   hook_entry, . - hook_entry
