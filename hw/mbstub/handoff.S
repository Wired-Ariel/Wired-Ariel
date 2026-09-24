@ =============================================================================
@ handoff.S - il "nuovo crt0": consegna il controllo a Smeraldo senza che il
@             gioco azzeri la EWRAM, e con il nostro payload gia' agganciato.
@ =============================================================================
@
@ PERCHE' ESISTE (la scoperta che riapre il multiboot)
@ ----------------------------------------------------
@ La nona sessione aveva archiviato il multiboot cosi': "il salto a 0x08000000
@ fa ripartire il gioco da zero, Emerald azzera la EWRAM". E' vero SOLO se si
@ salta all'entry point della cartuccia. Dal sorgente della decomp:
@
@   src/crt0.s:18-21   il RegisterRamReset e' dentro .if MODERN -> nella build
@                      retail crt0 NON azzera niente
@   src/main.c:93-95   l'azzeramento e' la PRIMA ISTRUZIONE di AgbMain
@
@ Quindi chi entra DENTRO AgbMain, oltre quella chiamata, non azzera mai la
@ EWRAM. Questo file e' esattamente quel "chi".
@
@ DOVE GIRA
@ ---------
@ A HANDOFF_BASE, cioe' dentro la coda di EWRAM che la misura E-0 ha dimostrato
@ intatta (0x0203D000-0x02040000, zero scritture del gioco in due sessioni di
@ gioco intere). Deve stare li' e non altrove: e' il codice che azzera la EWRAM,
@ quindi non puo' abitare la parte che azzera, e non puo' stare in IWRAM perche'
@ il RegisterRamReset del BIOS azzera anche quella.
@
@ ORDINE DELLE OPERAZIONI, e perche' e' quello
@ --------------------------------------------
@   1. IRQ off. Da qui alla fine non deve scattare niente.
@   2. Stack come src/crt0.s:28-29. PRIMA del SWI, non dopo: il RegisterRamReset
@      del BIOS gira sullo stack corrente e azzera la IWRAM tranne gli ultimi
@      0x200 byte - e sp_sys/sp_irq del gioco cadono dentro quella fascia
@      protetta. E' anche il motivo del commento in main.c:91-92 della decomp.
@   3. RegisterRamReset(0xFE) = RESET_ALL senza RESET_EWRAM.
@   4. EWRAM azzerata a mano da 0x02000000 a PAYLOAD_BASE. Questo e' il pezzo
@      che il BIOS avrebbe fatto e che facciamo noi, saltando la coda.
@   5. INTR_VECTOR = IntrMain (ROM), come src/crt0.s:15-17.
@   6. Il prologo di AgbMain 0x080003B0..0x080003CA eseguito da noi, chiamando
@      le stesse tre funzioni del gioco.
@   7. L'hook, che e' il motivo di tutto: dopo InitIntrHandlers INTR_VECTOR vale
@      IntrMain_Buffer, quindi lo si legge, lo si salva in g_origHandler e ci si
@      mette il payload. Concatenato, non sostituito.
@   8. Salto a 0x080003CE, dentro AgbMain, subito dopo bl InitIntrHandlers.
@
@ Da li' in poi il gioco parte normalmente e non sa niente.

#include "boot_syms_it.h"

    .section .handoff, "ax"
    .arm
    .align 2
    .global handoff_entry
handoff_entry:
    @ --- 1. IRQ off ---------------------------------------------------------
    ldr     r1, =REG_IME_ADDR
    mov     r0, #0
    strh    r0, [r1]

    @ --- 2. stack del gioco (src/crt0.s:9-14) -------------------------------
    @ 0x12 e 0x1F sono gli stessi valori di PSR_IRQ_MODE e PSR_SYS_MODE in
    @ constants/gba_constants.inc:6,10 - verificati, non ricopiati a memoria.
    @ ATTENZIONE al bit I: in entrambi vale 0, quindi gli IRQ restano ABILITATI
    @ a livello di CPU. Non e' una svista ereditata: e' cio' che fa il gioco, e
    @ l'unica cosa che tiene fermi gli interrupt da qui a InitIntrHandlers e'
    @ REG_IME, azzerato al passo 1. Chi tocca questa sequenza tenga presente che
    @ l'unica saracinesca e' quella.
    mov     r0, #0x12                       @ PSR_IRQ_MODE
    msr     cpsr_c, r0
    ldr     sp, =GAME_SP_IRQ
    mov     r0, #0x1F                       @ PSR_SYS_MODE
    msr     cpsr_c, r0
    ldr     sp, =GAME_SP_SYS

    @ --- 3. RegisterRamReset(RESET_ALL & ~RESET_EWRAM) ----------------------
    @ 0xFE = tutto tranne RESET_EWRAM (0x01). In ARM il numero della SWI sta nel
    @ campo commento, spostato di 16 bit: e' la stessa forma di src/crt0.s:20.
    mov     r0, #0xFE
    svc     #0x010000

    @ --- 4. EWRAM del gioco a zero, coda esclusa ----------------------------
    @ 0x02000000..PAYLOAD_BASE. Sono 0x3D000 byte, multiplo esatto di 16.
    ldr     r0, =0x02000000
    ldr     r1, =PAYLOAD_BASE
    mov     r2, #0
    mov     r3, #0
    mov     r4, #0
    mov     r5, #0
1:  stmia   r0!, {r2-r5}
    cmp     r0, r1
    blo     1b

    @ --- 5. INTR_VECTOR = IntrMain in ROM (src/crt0.s:15-17) ----------------
    ldr     r0, =ADDR_INTR_MAIN
    ldr     r1, =INTR_VECTOR
    str     r0, [r1]

    @ --- 6. il prologo di AgbMain, da 0x080003B0 a 0x080003CA ---------------
    @ *(vu16 *)BG_PLTT = RGB_WHITE
    @ 0x7FFF non e' un immediato ARM codificabile: va dal literal pool.
    ldr     r0, =BG_PLTT_ADDR
    ldr     r1, =RGB_WHITE_VALUE
    strh    r1, [r0]

    @ InitGpuRegManager(). ARMv4T non ha blx: il giro e' mov lr,pc + bx, e
    @ lr prende pc+8 con bit 0 a zero, quindi si torna qui in ARM.
    ldr     r0, =(ADDR_INIT_GPU_REG_MGR + 1)
    mov     lr, pc
    bx      r0

    @ REG_WAITCNT = 0x4014
    ldr     r0, =REG_WAITCNT_ADDR
    ldr     r1, =REG_WAITCNT_VALUE
    strh    r1, [r0]

    @ InitKeys()
    ldr     r0, =(ADDR_INIT_KEYS + 1)
    mov     lr, pc
    bx      r0

    @ InitIntrHandlers(). Da qui INTR_VECTOR vale IntrMain_Buffer e REG_IME
    @ vale 1: la funzione riabilita gli interrupt prima di tornare.
    ldr     r0, =(ADDR_INIT_INTR_HANDLERS + 1)
    mov     lr, pc
    bx      r0

    @ --- 7. l'hook ----------------------------------------------------------
    @ IRQ di nuovo giu' per la durata delle due scritture: fra il momento in cui
    @ g_origHandler e' scritto e quello in cui il vettore punta al payload non
    @ deve passare un VBlank, o l'hook girerebbe con l'handler originale ancora
    @ a zero e il tail jump finirebbe a 0x00000000.
    ldr     r1, =REG_IME_ADDR
    mov     r0, #0
    strh    r0, [r1]

    ldr     r1, =INTR_VECTOR
    ldr     r0, [r1]                        @ = IntrMain_Buffer
    ldr     r2, =PAYLOAD_BASE
    str     r0, [r2, #PAYLOAD_OFF_ORIG]     @ g_origHandler = vecchio vettore
    str     r2, [r1]                        @ INTR_VECTOR   = payload

    @ REG_IME come lo lascia InitIntrHandlers, cioe' 1: al rientro AgbMain deve
    @ trovare lo stato che avrebbe trovato senza di noi.
    ldr     r1, =REG_IME_ADDR
    mov     r0, #1
    strh    r0, [r1]

    @ --- 8. dentro AgbMain --------------------------------------------------
    @ Il prologo di AgbMain (push {r4-r7,lr} + push {r7}) non e' stato eseguito.
    @ AgbMain non ritorna mai - finisce in for(;;) - quindi quei registri non
    @ verranno mai ripescati, ma lo stack si tiene identico per non lasciare in
    @ giro una differenza che un giorno qualcuno dovrebbe spiegarsi.
    sub     sp, sp, #AGBMAIN_PROLOGUE_BYTES

    ldr     r0, =(ADDR_AGBMAIN_AFTER_INTR + 1)
    bx      r0

    .ltorg
    .global handoff_end
handoff_end:
