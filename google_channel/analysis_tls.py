"""Optional internal analysis listener; never changes legacy execution ports."""
import ssl


def analysis_tls_options(env):
    cert = env.get('GOOGLE_ANALYSIS_TLS_CERT_FILE', '')
    key = env.get('GOOGLE_ANALYSIS_TLS_KEY_FILE', '')
    if not cert and not key:
        return None
    token = env.get('GOOGLE_ANALYSIS_TOKEN', '')
    if not cert or not key or len(token) < 32 or any(c.isspace() for c in token):
        raise ValueError('invalid_analysis_tls_configuration')
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.load_cert_chain(cert, key)
    except (OSError, ValueError, ssl.SSLError):
        # Paths and certificate/key parse details are not safe startup logs.
        raise ValueError('invalid_analysis_tls_configuration') from None
    return {'ssl_certfile': cert, 'ssl_keyfile': key,
            'ssl_version': ssl.PROTOCOL_TLS_SERVER}
