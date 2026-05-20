from scrapy.downloadermiddlewares.retry import RetryMiddleware


class InstagramRetryMiddleware(RetryMiddleware):
    def _retry(self, request, reason, spider=None):
        try:
            retry_request = super()._retry(request, reason)
        except TypeError:  # pragma: no cover
            retry_request = super()._retry(request, reason, spider)

        if retry_request is not None:
            attempt_number = retry_request.meta.get("retry_times", 0)
            active_spider = spider or getattr(getattr(self, "crawler", None), "spider", None)
            if active_spider is not None:
                active_spider.logger.warning(
                    "Request retry %s/%s for %s (reason: %s)",
                    attempt_number,
                    self.max_retry_times,
                    request.url,
                    reason,
                )
        return retry_request
