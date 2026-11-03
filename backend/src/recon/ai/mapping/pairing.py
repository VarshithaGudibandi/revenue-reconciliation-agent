from collections.abc import Callable

from recon.ai.mapping.profile import FileProfile
from recon.ai.mapping.proposal import FileProposal, MappingProposal

Mapper = Callable[[FileProfile, FileProfile], MappingProposal]
SingleFileMapper = Callable[[FileProfile, str], FileProposal]


def each_file_alone(mapper: SingleFileMapper) -> Mapper:
    """Use a one-file mapper as a mapper of two files, giving each no sight of the other."""

    def paired(invoices: FileProfile, payments: FileProfile) -> MappingProposal:
        return MappingProposal(
            invoices=mapper(invoices, "invoices"), payments=mapper(payments, "payments")
        )

    return paired
