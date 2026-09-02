# Pairing Flow

1. User sends `/pair <username>`.
2. Dischat checks the persistent rate limit for the Matrix user and the
   requested Discourse username. If the issuance window is exhausted or a
   cooldown is active, the command is rejected with a clear "try again in N
   minutes" message and no Discourse PM is sent.
3. Dischat generates a 6-digit code.
4. Dischat sends the code to the Discourse user by private message.
5. User replies in Matrix with the plain code.
6. Dischat validates the hashed code and links the MXID. Verification is gated
   only by active cooldowns — the issuance window never blocks a user from
   entering a code they were already sent. Failed verification attempts count
   toward a persistent limit that survives session replacement; when the
   threshold is reached, verification and new `/pair` starts are blocked until
   the cooldown expires. Once a cooldown expires it re-arms normally: the
   failure counter resets, and the next `max_failures` failed attempts after
   expiry trigger a fresh cooldown.
