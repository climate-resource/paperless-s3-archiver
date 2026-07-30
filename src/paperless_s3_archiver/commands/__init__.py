"""
The subcommands, one module each

Every command is a function of ``(cfg, args)`` returning a process exit status,
and every one of them writes its own metrics file before returning -- including
on the failure path, because a job that fails silently and a job that never ran
look identical to an alert rule otherwise.
"""
