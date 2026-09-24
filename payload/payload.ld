/* =============================================================================
 * payload.ld — colloca il payload a un indirizzo fisso in EWRAM
 * =============================================================================
 *
 * PAYLOAD_BASE arriva dalla riga di comando (--defsym): build.ps1 passa
 * 0x0203CF80, subito sopra la fine della sezione `ewram` del gioco
 * (0x0203CF64, dal .map di pokeemerald e confermata sulla ROM italiana da
 * tools/ewram_bound.py). Il default qui sotto e' solo il ripiego di un link
 * lanciato a mano senza --defsym, e build.ps1 lo rifiuta comunque (guardia
 * sul confine del gioco).
 *
 * Layout garantito, su cui si basano hook.S e l'iniettore Lua:
 *   +0x00  b hook_entry      (il vettore IRQ 0x03007FFC punta qui)
 *   +0x04  g_origHandler     (scritto dall'iniettore)
 *   +0x08  g_magic           ('HOOK')
 *   +0x10  g_state           (struct PayloadState, letta dall'harness Lua)
 *   segue  g_mailbox         (coda di eventi di rete; l'offset lo legge
 *                             build.ps1 dall'ELF -> PAYLOAD_OFF_MAILBOX)
 *   dopo   codice e dati, poi lo stack privato dell'IRQ
 *
 * Solo i primi tre campi e l'inizio di g_state sono un contratto fisso con
 * l'iniettore Lua. Il codice viene dopo e puo' spostarsi liberamente.
 */

ENTRY(_start)

__base = DEFINED(PAYLOAD_BASE) ? PAYLOAD_BASE : 0x02030000;

SECTIONS
{
    . = __base;

    .header : {
        KEEP(*(.text.start))
    }

    .state __base + 0x10 : {
        KEEP(*(.payload_state))
    }

    /* Casella postale fra payload e mondo esterno (harness Lua ora, firmware
     * Celio in Fase 5).
     *
     * NON PIU' A INDIRIZZO FISSO, dal 2026-08-02. Stava a +0x200 mentre
     * PayloadState finiva a +0x168: 152 byte di buco, tenuti apposta perche' lo
     * stato potesse crescere senza collidere. Con il driver SIO dentro, quei
     * 152 byte servono.
     *
     * Ora la mailbox SEGUE lo stato, allineata a 8. Il buco sparisce e il
     * problema della crescita anche: se PayloadState si allunga, la mailbox si
     * sposta da sola. Il prezzo e' che l'offset non e' piu' una costante
     * scrivibile a mano da due parti - e infatti non lo e': build.ps1 lo legge
     * dall'ELF e lo emette nello script Lua generato (PAYLOAD_OFF_MAILBOX), che
     * mgba/inject_body.lua consuma. Una sola fonte, quella vera. */
    . = ALIGN(8);
    .mailbox : {
        KEEP(*(.payload_mailbox))
    }

    /* Il codice segue la struttura di stato: quest'ultima cresce quando si
     * aggiungono campi da osservare, e non deve collidere con .text. */
    .text : {
        *(.text*)
        *(.rodata*)
        . = ALIGN(4);
    }

    .data : {
        *(.data*)
        . = ALIGN(4);
    }

    .bss (NOLOAD) : {
        *(.bss*)
        *(COMMON)
        . = ALIGN(8);
    }

    /* payload_end e' la fine di CODICE + DATI + BSS, e deve stare sotto
     * HANDOFF_BASE (0x0203FE00): da li' in su mbstub copia handoff.S, e una
     * bss che ci finisse sotto verrebbe SOVRASCRITTA dal codice di handoff
     * dopo l'azzeramento - variabili "a zero" piene di machine code. La
     * guardia sta in build.ps1. */
    payload_end = .;

    /* Stack privato del payload: il codice C non puo' girare sullo stack IRQ,
     * che e' di circa 0x60 byte.
     *
     * DAL 2026-08-25 STA IN CIMA ALLA EWRAM, DENTRO LA REGIONE DI HANDOFF
     * (0x0203FE00-0x02040000). Non e' un azzardo, e' un fatto d'ordine:
     * handoff.S gira UNA volta, al boot, prima che il gioco parta - e il
     * primo IRQ che usa questo stack arriva molto dopo, quando quei byte
     * sono codice morto che nessuno rieseguira' mai. In emulatore la regione
     * e' semplicemente libera (l'iniezione Lua non ha handoff). Uno stack
     * non ha bisogno di init: si scrive prima di leggersi.
     *
     * CHI CI GIRA SOPRA (2026-08-23): SOLO il percorso IRQ - hook.S (8 byte)
     * -> payload_frame() -> payload_drain(). Il corpo del payload gira in
     * payload_cb1 sullo stack del gioco. Il consumo non e' una misura, e' un
     * LIMITE calcolato da GCC (-fstack-usage + -fcallgraph-info) e sommato
     * da build.ps1 lungo il grafo: la build fallisce sopra la meta', o se
     * dall'IRQ parte una chiamata indiretta. Il canary in mGBA resta come
     * controprova.
     *
     * Storia: 0x400 all'inizio, 0x2E0 il 2026-08-16, 0x140 il 2026-08-23
     * (l'IRQ fa solo SIO, contatori, latch e scudo), trasloco in cima il
     * 2026-08-25 per far posto ai 4 giocatori sotto HANDOFF_BASE. */
    payload_stack_bottom = 0x02040000 - 0x120;
    payload_stack_top = 0x02040000;

    /* .lateclear: dati AD AZZERAMENTO TARDIVO, anche loro dentro la regione
     * di handoff. mbstub li azzererebbe e poi ci copierebbe sopra handoff.S:
     * quindi NON possono contare sull'azzeramento del caricatore, e infatti
     * non lo fanno - li azzera RemotesInitOnce al primo VBlank, quando
     * handoff e' gia' codice morto e nessuno li ha ancora letti. Ci vanno
     * SOLO dati che il payload tocca dopo quel momento (oggi: le code
     * per-slot della RX). La guardia in build.ps1 controlla che non
     * sconfinino nello stack. */
    lateclear_start = 0x0203FE00;
    .lateclear lateclear_start (NOLOAD) : {
        *(.lateclear)
    }
    lateclear_end = lateclear_start + SIZEOF(.lateclear);

    /* =========================================================================
     * NIENTE IN IWRAM. Mai.
     * =========================================================================
     *
     * Il 2026-08-02 il .bss del driver SIO era stato messo a 0x030078AC, sopra
     * la sezione `iwram` del gioco, sulla base di una misura: 684 byte visti
     * liberi in una sessione con lotta e salvataggio.
     *
     * E' stato tolto, e la ragione va tenuta a mente perche' la tentazione
     * tornera' al prossimo byte che manca. Sopra 0x030078AC ci sono gli STACK
     * del gioco (sp_sys da 0x03007E40, sp_irq da 0x03007FA0, entrambi verso il
     * basso). Il gioco e' AUTORIZZATO a scendere fino a 0x030078AC: se scendesse
     * oltre corromperebbe i propri dati, quindi sappiamo che sta sopra quel
     * confine - ma niente garantisce che stia sopra 0x030078AC + quanto ci
     * mettiamo noi. Una misura dice cosa e' successo in una sessione; solo il
     * linker dice cosa PUO' succedere, e per gli stack il linker non dice
     * niente.
     *
     * In EWRAM invece il confine e' un fatto: nel .map non esiste un simbolo
     * sopra 0x0203CF64, e il gioco non ha crescita dinamica (tutti gli InitHeap
     * passano gHeap/HEAP_SIZE, un array statico dentro la sezione). Verificato
     * anche sulla ROM italiana in modo esaustivo: tools/ewram_bound.py cerca
     * ogni indirizzo EWRAM materializzato da una load PC-relative dentro il
     * codice, e il massimo e' 0x0203CF60 su ENTRAMBE le ROM.
     *
     * build.ps1 fallisce se un simbolo del payload finisce in IWRAM.
     */

    /DISCARD/ : { *(.comment) *(.ARM.attributes) *(.note*) }
}
