import logging

# logger = logging.getLogger('db')
console_logger = logging.getLogger('root')


def error(msg, *args, **kwargs):
    # log_func(msg,l.critical)
    console_logger.error("ERR:" + msg)
    # logger.error("ERR:" + msg, *args, extra=kwargs)


def warning(msg, *args, **kwargs):
    console_logger.warning("WRN:" + msg)
    # logger.warning("WRN:" + msg, *args, extra=kwargs)


def info(msg, *args, **kwargs):
    console_logger.info(msg)
    # logger.info(msg, *args, extra=kwargs)


def debug(msg, *args, **kwargs):
    console_logger.debug(msg)


def critical(msg, *args, **kwargs):
    console_logger.critical("CRIT:" + msg)


def exception(msg, *args, **kwargs):
    console_logger.exception("EXEPTION:" + msg)
