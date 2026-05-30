# Pairing Flow

1. User sends `/pair <username>`.
2. Dischat generates a 6-digit code.
3. Dischat sends the code to the Discourse user by private message.
4. User replies in Matrix with the plain code.
5. Dischat validates the hashed code and links the MXID.
