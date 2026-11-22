# shk site environment, put in front of the text of a program for a site whose Slurm clients are
# not on the login PATH; run as sh -c TEXT shk-NAME BIN CONF ARGS... BIN is appended to PATH,
# never prepended, so earlier entries keep winning; CONF becomes SLURM_CONF, which the scrub of
# every program keeps; the shift leaves exactly ARGS as the positional parameters of the program.
PATH=$PATH:$1
SLURM_CONF=$2
export PATH SLURM_CONF
shift 2
