class ProviderError(ValueError):
    """Public, credential-free platform error."""

    def __init__(self, code, message, status_code=422):
        super().__init__(message)
        self.code = code
        self.status_code = status_code

    def detail(self):
        return {'error_code': self.code, 'message': str(self)}
